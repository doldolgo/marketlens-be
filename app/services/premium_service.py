"""김치 프리미엄 계산 서비스.

    premium_ratio = KRW 가격 / USDT 가격 / 환율

KRW 쪽은 항상 원화 기준 거래소(기본값 업비트) 하나로 고정된다.
USDT 쪽은 요청한 거래소들이며, 생략하면 USDT 마켓을 지원하는 모든 거래소를 쓴다.

가격 기준(``PriceBasis``)
    - ``last`` (기본값): 마지막 체결가. 통상적인 '현재가' 정의이며 일반적인 김프 계산 방식.
    - ``mid``: 최우선 매수/매도 호가의 중간값. 체결이 뜸한 종목에서도 항상 최신.

세 가격(원화·해외·환율)은 **반드시 같은 기준**으로 뽑는다. 한쪽만 체결가를 쓰면
스프레드만큼의 편향이 그대로 프리미엄에 섞여 들어간다.

동작 순서
    1. 원화 가격 · 환율 · 해외 가격을 모두 asyncio.gather 로 동시에 조회한다.
    2. 원화 가격이 없으면 기준이 사라지므로 즉시 실패한다.
    3. 거래소별로 비율을 계산해 프리미엄이 큰 순서로 정렬한다.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from app.core.config import settings
from app.core.errors import ExchangeAPIError, MarketLensError, UnsupportedMarketError
from app.exchanges.registry import all_exchanges, get_exchange
from app.models.orderbook import MarketType, OrderBook
from app.models.premium import PremiumEntry, PremiumFailure, PremiumResult
from app.models.symbol import Symbol
from app.models.ticker import PriceBasis, Ticker
from app.services.fx import FxRate, fx_service
from app.services.market_data_service import market_data_service


@dataclass(frozen=True, slots=True)
class PricePoint:
    """가격 기준에 무관하게 통일된 '거래소 하나의 가격'.

    OrderBook(중간가) 과 Ticker(체결가) 를 같은 형태로 눕혀서
    아래 계산 로직이 둘을 구분하지 않아도 되게 한다.
    """

    exchange: str
    symbol: str
    native_symbol: str
    market_type: MarketType
    quote: str
    price: float
    timestamp: int
    latency_ms: float

    @classmethod
    def from_source(cls, source: OrderBook | Ticker) -> "PricePoint":
        """OrderBook 이면 중간가를, Ticker 면 마지막 체결가를 꺼낸다."""
        price = source.mid_price if isinstance(source, OrderBook) else source.last_price

        if price is None or price <= 0:
            raise ExchangeAPIError(
                f"{source.exchange} {source.native_symbol} 시세에서 가격을 알 수 없습니다.",
                detail={"exchange": source.exchange, "native_symbol": source.native_symbol},
            )

        return cls(
            exchange=source.exchange,
            symbol=source.symbol,
            native_symbol=source.native_symbol,
            market_type=source.market_type,
            quote=source.quote,
            price=price,
            timestamp=source.timestamp,
            latency_ms=source.latency_ms,
        )


class PremiumService:
    """원화 가격과 해외 가격의 괴리를 계산한다."""

    @property
    def krw_exchange_id(self) -> str:
        return settings.krw_reference_exchange

    def resolve_targets(
        self,
        base: str,
        exchanges: list[str] | None,
        market_type: MarketType,
    ) -> list[tuple[str, Symbol, MarketType]]:
        """비교 대상 (거래소, 심볼, 시장구분) 목록을 만든다.

        Args:
            base: 코인 심볼.
            exchanges: 비교할 거래소 ID 목록. ``None`` 이면 USDT 마켓을 지원하는
                모든 거래소에서 원화 기준 거래소를 뺀 나머지.
            market_type: 현물/선물 구분.

        Raises:
            UnsupportedMarketError: 명시한 거래소가 USDT 마켓을 지원하지 않는 경우.
        """
        symbol = Symbol(base=base.upper(), quote=settings.fx_stablecoin)

        if exchanges is None:
            # 자동 선택: 원화 기준 거래소는 프리미엄의 기준점이므로 대상에서 뺀다.
            candidates = [
                exchange
                for exchange in all_exchanges()
                if exchange.id != self.krw_exchange_id
                and exchange.supports(symbol, market_type)
            ]
        else:
            candidates = []
            for exchange_id in exchanges:
                exchange = get_exchange(exchange_id)
                # 명시적으로 요청했다면 원화 기준 거래소도 대상에 넣는다.
                # (업비트 KRW 마켓 vs 업비트 USDT 마켓 = 거래소 내부 테더 괴리)
                exchange.ensure_supported(symbol, market_type)
                candidates.append(exchange)

        return [(exchange.id, symbol, market_type) for exchange in candidates]

    def _build_entry(self, point: PricePoint, krw_price: float, fx: FxRate) -> PremiumEntry:
        """거래소 하나의 프리미엄을 계산한다."""
        price_in_krw = point.price * fx.rate

        # 핵심 계산: KRW 가격 / 해외 가격 / 환율
        ratio = krw_price / point.price / fx.rate

        return PremiumEntry(
            exchange=point.exchange,
            name=get_exchange(point.exchange).name,
            symbol=point.symbol,
            native_symbol=point.native_symbol,
            market_type=point.market_type,
            quote_currency=point.quote,
            price=point.price,
            price_in_krw=price_in_krw,
            premium_ratio=ratio,
            premium_percent=(ratio - 1) * 100,
            premium_krw=krw_price - price_in_krw,
            timestamp=point.timestamp,
            latency_ms=point.latency_ms,
        )

    async def _fetch_krw_price(self, base: str, basis: PriceBasis) -> PricePoint:
        """원화 기준 거래소의 가격을 가져온다."""
        exchange = get_exchange(self.krw_exchange_id)
        symbol = Symbol(base=base.upper(), quote=settings.krw_reference_quote)

        if not exchange.supports(symbol, MarketType.SPOT):
            raise UnsupportedMarketError(
                f"{exchange.name}는 {symbol.quote} 마켓을 지원하지 않습니다.",
                detail={"exchange": exchange.id, "quote": symbol.quote},
            )

        if basis is PriceBasis.LAST:
            source = await exchange.fetch_ticker(symbol, market_type=MarketType.SPOT)
        else:
            source = await exchange.fetch_orderbook(
                symbol, depth=1, market_type=MarketType.SPOT
            )

        return PricePoint.from_source(source)

    async def _fetch_overseas(
        self,
        targets: list[tuple[str, Symbol, MarketType]],
        basis: PriceBasis,
    ):
        """해외 거래소 가격을 동시에 조회한다."""
        if basis is PriceBasis.LAST:
            return await market_data_service.fetch_tickers(targets)
        return await market_data_service.fetch_orderbooks(targets, depth=1)

    async def fetch_premiums(
        self,
        base: str,
        *,
        exchanges: list[str] | None = None,
        market_type: MarketType = MarketType.SPOT,
        price_basis: PriceBasis = PriceBasis.LAST,
    ) -> PremiumResult:
        """코인 하나의 거래소별 프리미엄을 계산한다.

        Args:
            base: 코인 심볼 (예: "BTC").
            exchanges: 비교할 거래소 ID 목록. 생략하면 지원되는 전체.
            market_type: 현물/선물 구분.
            price_basis: 가격 기준. ``last`` (마지막 체결가) 또는 ``mid`` (호가 중간가).

        Raises:
            MarketLensError: 원화 기준 가격이나 환율을 가져오지 못한 경우.
                이 둘은 계산의 기준이라 하나라도 없으면 아무것도 계산할 수 없다.
        """
        started = time.perf_counter()
        targets = self.resolve_targets(base, exchanges, market_type)

        # 원화 가격 · 환율 · 해외 가격을 모두 동시에, 같은 기준으로 요청한다.
        krw_point, fx, (sources, fetch_failures) = await asyncio.gather(
            self._fetch_krw_price(base, price_basis),
            fx_service.fetch_rate(price_basis),
            self._fetch_overseas(targets, price_basis),
        )

        entries: list[PremiumEntry] = []
        failures = [
            PremiumFailure(
                exchange=f.exchange,
                symbol=f.symbol,
                error_code=f.error_code,
                message=f.message,
            )
            for f in fetch_failures
        ]

        for source in sources:
            try:
                entries.append(
                    self._build_entry(PricePoint.from_source(source), krw_point.price, fx)
                )
            except MarketLensError as exc:
                failures.append(
                    PremiumFailure(
                        exchange=source.exchange,
                        symbol=source.symbol,
                        error_code=exc.code,
                        message=exc.message,
                    )
                )

        entries.sort(key=lambda e: e.premium_percent, reverse=True)

        return PremiumResult(
            base=base.upper(),
            price_basis=price_basis,
            krw_exchange=krw_point.exchange,
            krw_symbol=krw_point.symbol,
            krw_native_symbol=krw_point.native_symbol,
            krw_price=krw_point.price,
            krw_timestamp=krw_point.timestamp,
            usdt_krw_rate=fx.rate,
            fx_source=fx.source,
            premiums=entries,
            failures=failures,
            fetched_at=int(time.time() * 1000),
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
        )


premium_service = PremiumService()
