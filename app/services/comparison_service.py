"""거래소 간 가격 비교 서비스.

동작 순서
    1. 요청받은 코인에 대해 각 거래소의 기본 마켓을 결정한다 (업비트 KRW, 바이낸스 USDT).
    2. 모든 거래소 호가 + 환율을 asyncio.gather 로 동시에 조회한다.
    3. 모든 가격을 공통 통화로 환산한다.
    4. 최저 매수처 / 최고 매도처를 찾아 스프레드를 계산한다.
"""

from __future__ import annotations

import asyncio
import time

from app.core.errors import ExchangeAPIError
from app.exchanges.registry import exchange_ids, get_exchange
from app.models.comparison import (
    ArbitrageSpread,
    ComparisonResult,
    ExchangeFailure,
    ExchangeQuote,
)
from app.models.orderbook import MarketType, OrderBook
from app.models.symbol import Symbol
from app.services.fx import fx_service
from app.services.market_data_service import market_data_service

#: 공통 통화로 환산 가능한 결제 통화. (BTC/ETH 마켓 등은 아직 지원하지 않는다)
_CONVERTIBLE = frozenset({"KRW", "USDT"})


class ComparisonService:
    """여러 거래소의 같은 코인 가격을 공통 통화 기준으로 비교한다."""

    def resolve_targets(
        self,
        base: str,
        exchanges: list[str] | None = None,
        market_type: MarketType = MarketType.SPOT,
    ) -> list[tuple[str, Symbol, MarketType]]:
        """비교 대상 (거래소, 심볼, 시장구분) 목록을 만든다.

        각 거래소의 ``default_quote`` 를 사용하므로, 사용자는 거래소마다
        결제 통화가 다르다는 사실을 신경 쓸 필요가 없다.
        """
        ids = exchanges or exchange_ids()
        targets: list[tuple[str, Symbol, MarketType]] = []

        for exchange_id in ids:
            exchange = get_exchange(exchange_id)
            symbol = Symbol(base=base.upper(), quote=exchange.default_quote)
            targets.append((exchange.id, symbol, market_type))

        return targets

    def _conversion_factor(
        self, quote_currency: str, common_currency: str, usdt_krw: float
    ) -> float:
        """``quote_currency`` 가격에 곱하면 ``common_currency`` 가격이 되는 계수."""
        if quote_currency not in _CONVERTIBLE or common_currency not in _CONVERTIBLE:
            raise ExchangeAPIError(
                f"{quote_currency} -> {common_currency} 환산을 지원하지 않습니다. "
                f"환산 가능 통화: {', '.join(sorted(_CONVERTIBLE))}",
                detail={"quote": quote_currency, "common_currency": common_currency},
            )
        if quote_currency == common_currency:
            return 1.0
        # 일단 KRW 축으로 올린 뒤, 기준 통화가 USDT 면 다시 나눈다.
        to_krw = usdt_krw if quote_currency == "USDT" else 1.0
        return to_krw if common_currency == "KRW" else to_krw / usdt_krw

    def _to_quote(self, book: OrderBook, usdt_krw: float, common_currency: str) -> ExchangeQuote:
        """OrderBook 을 공통 통화로 환산한 ExchangeQuote 로 변환한다."""
        factor = self._conversion_factor(book.quote, common_currency, usdt_krw)

        best_bid = book.best_bid
        best_ask = book.best_ask
        mid = book.mid_price
        if best_bid is None or best_ask is None or mid is None:
            raise ExchangeAPIError(
                f"{book.exchange} 호가가 비어 있어 비교할 수 없습니다.",
                detail={"exchange": book.exchange, "native_symbol": book.native_symbol},
            )

        return ExchangeQuote(
            exchange=book.exchange,
            symbol=book.symbol,
            native_symbol=book.native_symbol,
            market_type=book.market_type,
            quote_currency=book.quote,
            best_bid=best_bid,
            best_ask=best_ask,
            mid_price=mid,
            best_bid_converted=best_bid * factor,
            best_ask_converted=best_ask * factor,
            mid_price_converted=mid * factor,
            timestamp=book.timestamp,
            latency_ms=book.latency_ms,
        )

    def _build_spread(self, quotes: list[ExchangeQuote]) -> ArbitrageSpread | None:
        """가장 싸게 사는 곳과 가장 비싸게 파는 곳의 차이를 계산한다.

        비교 가능한 거래소가 2곳 미만이면 None 을 반환한다.
        수수료/출금비용/전송시간은 반영하지 않은 이론값이다.
        """
        if len(quotes) < 2:
            return None

        buy = min(quotes, key=lambda q: q.best_ask_converted)
        sell = max(quotes, key=lambda q: q.best_bid_converted)

        if buy.exchange == sell.exchange:
            return None

        absolute = sell.best_bid_converted - buy.best_ask_converted
        percent = (absolute / buy.best_ask_converted) * 100 if buy.best_ask_converted else 0.0

        return ArbitrageSpread(
            buy_exchange=buy.exchange,
            buy_price=buy.best_ask_converted,
            sell_exchange=sell.exchange,
            sell_price=sell.best_bid_converted,
            absolute=absolute,
            percent=percent,
        )

    async def compare(
        self,
        base: str,
        *,
        exchanges: list[str] | None = None,
        common_currency: str = "KRW",
        market_type: MarketType = MarketType.SPOT,
        depth: int = 1,
    ) -> ComparisonResult:
        """거래소 간 가격을 비교한다.

        Args:
            base: 비교할 코인 (예: "BTC").
            exchanges: 비교할 거래소 ID 목록. 생략하면 등록된 전체.
            common_currency: 환산 기준 통화 ("KRW" 또는 "USDT").
            market_type: 현물/선물 구분.
            depth: 조회 호가 단계 수. 비교에는 1단계면 충분하다.
        """
        started = time.perf_counter()
        currency = common_currency.upper()
        if currency not in _CONVERTIBLE:
            raise ExchangeAPIError(
                f"지원하지 않는 비교 기준 통화입니다: {currency}. "
                f"{' 또는 '.join(sorted(_CONVERTIBLE))} 만 지원합니다.",
                detail={"common_currency": currency},
            )

        targets = self.resolve_targets(base, exchanges, market_type)

        # 호가 조회와 환율 조회를 동시에 시작한다.
        (books, failures), fx = await asyncio.gather(
            market_data_service.fetch_orderbooks(targets, depth=max(depth, 1)),
            fx_service.fetch_rate(),
        )
        usdt_krw, fx_source = fx.rate, fx.source

        quotes: list[ExchangeQuote] = []
        failures = list(failures)

        for book in books:
            try:
                quotes.append(self._to_quote(book, usdt_krw, currency))
            except ExchangeAPIError as exc:
                failures.append(
                    ExchangeFailure(
                        exchange=book.exchange,
                        symbol=book.symbol,
                        error_code=exc.code,
                        message=exc.message,
                    )
                )

        quotes.sort(key=lambda q: q.mid_price_converted)

        return ComparisonResult(
            base=base.upper(),
            common_currency=currency,
            usdt_krw_rate=usdt_krw,
            fx_source=fx_source,
            quotes=quotes,
            failures=failures,
            spread=self._build_spread(quotes),
            fetched_at=int(time.time() * 1000),
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
        )


comparison_service = ComparisonService()
