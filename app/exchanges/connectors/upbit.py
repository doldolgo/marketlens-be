"""업비트(Upbit) 커넥터.

원본 엔드포인트
    GET https://api.upbit.com/v1/orderbook?markets=KRW-BTC
    - 인증 불필요 (public quotation API)
    - Rate limit: 초당 10회 (Remaining-Req 응답 헤더로 잔여량 확인 가능)
    - level 파라미터로 호가 모아보기 단위를 지정할 수 있으나 기본값(0)을 사용한다.

업비트는 요청 시 호가 개수를 지정할 수 없고 항상 최대 30단계를 내려주므로,
``depth`` 는 응답을 받은 뒤 잘라내는 방식으로 적용한다.
"""

from __future__ import annotations

from typing import Any, ClassVar

from app.core.config import settings
from app.core.errors import ExchangeAPIError, MarketNotFoundError
from app.models.orderbook import MarketType, OrderBook, OrderBookLevel
from app.models.symbol import Symbol
from app.models.ticker import Ticker
from app.exchanges.base import BaseExchange




class Upbit(BaseExchange):
    id: ClassVar[str] = "upbit"
    name: ClassVar[str] = "업비트"
    quote_currencies: ClassVar[frozenset[str]] = frozenset({"KRW", "BTC", "USDT"})
    default_quote: ClassVar[str] = "KRW"
    supported_market_types: ClassVar[frozenset[MarketType]] = frozenset({MarketType.SPOT})

    ORDERBOOK_PATH = "/v1/orderbook"
    TICKER_PATH = "/v1/ticker"

    def to_native_symbol(self, symbol: Symbol, market_type: MarketType) -> str:
        """BTC/KRW -> KRW-BTC (업비트는 QUOTE-BASE 순서)."""
        return f"{symbol.quote}-{symbol.base}"

    async def _get_market_json(self, path: str, native_symbol: str) -> Any:
        """마켓 코드를 넘기는 조회 API 공통 호출부.

        업비트는 없는 마켓에 404 + ``{"error": {"name": 404, ...}}`` 를 반환하므로
        이를 MarketNotFoundError 로 옮겨준다.
        """
        url = f"{settings.upbit_base_url}{path}"
        try:
            return await self._get_json(url, params={"markets": native_symbol})
        except ExchangeAPIError as exc:
            if exc.detail.get("status_code") == 404:
                raise MarketNotFoundError(
                    f"업비트에 {native_symbol} 마켓이 존재하지 않습니다.",
                    detail={"exchange": self.id, "native_symbol": native_symbol},
                ) from exc
            raise

    async def _request_orderbook(
        self, native_symbol: str, depth: int, market_type: MarketType
    ) -> Any:
        return await self._get_market_json(self.ORDERBOOK_PATH, native_symbol)

    async def _request_ticker(self, native_symbol: str, market_type: MarketType) -> Any:
        return await self._get_market_json(self.TICKER_PATH, native_symbol)

    def _parse_ticker(
        self,
        raw: Any,
        *,
        symbol: Symbol,
        native_symbol: str,
        market_type: MarketType,
        latency_ms: float,
    ) -> Ticker:
        if not isinstance(raw, list) or not raw:
            raise MarketNotFoundError(
                f"업비트에 {native_symbol} 마켓이 존재하지 않습니다.",
                detail={"exchange": self.id, "native_symbol": native_symbol},
            )

        t = raw[0]

        # trade_price / trade_timestamp 는 진짜 마지막 체결값이다. 실측 검증:
        # 33분간 거래가 없던 KRW-MOC 에서 /v1/trades/ticks 의 최신 체결과
        # 가격·시각이 정확히 일치했다.
        #
        # 응답에는 opening_price / high_price / signed_change_rate / acc_trade_* 등
        # 기간 요약도 함께 오지만 쓰지 않는다. 업비트의 그 값들은 00:00 UTC(=09:00 KST)
        # 기준 당일 구간이라 바이낸스의 롤링 24시간과 의미가 달라 비교할 수 없다.
        return Ticker(
            exchange=self.id,
            symbol=str(symbol),
            native_symbol=native_symbol,
            market_type=market_type,
            base=symbol.base,
            quote=symbol.quote,
            last_price=float(t["trade_price"]),
            timestamp=int(t.get("trade_timestamp", 0)),
            latency_ms=round(latency_ms, 2),
        )

    def _parse_orderbook(
        self,
        raw: Any,
        *,
        symbol: Symbol,
        native_symbol: str,
        market_type: MarketType,
        depth: int,
        latency_ms: float,
    ) -> OrderBook:
        if not isinstance(raw, list) or not raw:
            raise MarketNotFoundError(
                f"업비트에 {native_symbol} 마켓이 존재하지 않습니다.",
                detail={"exchange": self.id, "native_symbol": native_symbol},
            )

        book = raw[0]
        units = book.get("orderbook_units")
        if not units:
            raise ExchangeAPIError(
                f"업비트 호가 응답에 orderbook_units 가 없습니다: {native_symbol}",
                detail={"exchange": self.id, "native_symbol": native_symbol},
            )

        # 업비트는 하나의 unit 안에 같은 단계의 bid/ask 를 함께 담아 내려준다.
        # 이미 bid 는 내림차순, ask 는 오름차순으로 정렬되어 있다.
        bids = [
            OrderBookLevel(price=float(u["bid_price"]), size=float(u["bid_size"]))
            for u in units[:depth]
        ]
        asks = [
            OrderBookLevel(price=float(u["ask_price"]), size=float(u["ask_size"]))
            for u in units[:depth]
        ]

        return OrderBook(
            exchange=self.id,
            symbol=str(symbol),
            native_symbol=native_symbol,
            market_type=market_type,
            base=symbol.base,
            quote=symbol.quote,
            bids=bids,
            asks=asks,
            timestamp=int(book.get("timestamp", 0)),
            latency_ms=round(latency_ms, 2),
        )
