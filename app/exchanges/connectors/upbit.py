"""업비트(Upbit) 커넥터 — 독립 구현.

원본 엔드포인트 — 전부 인증 불필요 (public quotation API)
    GET https://api.upbit.com/v1/orderbook?markets=KRW-BTC
    GET https://api.upbit.com/v1/ticker?markets=KRW-BTC
    GET https://api.upbit.com/v1/market/all

- Rate limit: **엔드포인트 그룹별 초당 10회** (`Remaining-Req` 헤더로 잔여량 확인)
- 호가 개수를 요청할 수 없고 **항상 최대 30단계**를 내려주므로,
  ``depth`` 는 응답을 받은 뒤 잘라내는 방식으로 적용한다.
- ``markets`` 에 쉼표로 여러 마켓을 넘길 수 있다 (281개 KRW 마켓을 3회 호출로).

빗썸 v1 API 와 응답 형태가 같지만 **일부러 코드를 공유하지 않는다.**
어느 한쪽 API 가 바뀌었을 때 다른 쪽이 함께 흔들리는 구조를 피하기 위해
거래소마다 전체 구현을 따로 둔다.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, ClassVar

from app.core.config import settings
from app.core.errors import (
    ExchangeAPIError,
    MarketNotFoundError,
    UnsupportedMarketError,
)
from app.exchanges.base import BaseExchange
from app.models.bulk import BulkQuote
from app.models.orderbook import MarketType, OrderBook, OrderBookLevel
from app.models.symbol import Symbol
from app.models.ticker import Ticker


class Upbit(BaseExchange):
    id: ClassVar[str] = "upbit"
    name: ClassVar[str] = "업비트"
    quote_currencies: ClassVar[frozenset[str]] = frozenset({"KRW", "BTC", "USDT"})
    default_quote: ClassVar[str] = "KRW"
    supported_market_types: ClassVar[frozenset[MarketType]] = frozenset(
        {MarketType.SPOT}
    )
    supports_bulk: ClassVar[bool] = True
    #: orderbook 일괄 조회가 **30단계 전부**를 내려주므로 깊이 일괄이 가능하다.
    supports_bulk_depth: ClassVar[bool] = True
    is_domestic: ClassVar[bool] = True

    ORDERBOOK_PATH = "/v1/orderbook"
    TICKER_PATH = "/v1/ticker"
    MARKETS_PATH = "/v1/market/all"

    #: 한 번에 요청할 마켓 개수. URI 길이 제한(414) 때문에 나눠 보낸다.
    BULK_CHUNK = 100
    #: 마켓 목록 캐시 TTL (초).
    MARKETS_TTL = 600.0

    #: {결제통화: (마켓 목록, 캐시 시각)}
    _markets_cache: ClassVar[dict[str, tuple[list[str], float]]] = {}

    @property
    def base_url(self) -> str:
        return settings.upbit_base_url

    # ------------------------------------------------------------------
    # 심볼 · 요청
    # ------------------------------------------------------------------

    def to_native_symbol(self, symbol: Symbol, market_type: MarketType) -> str:
        """BTC/KRW -> KRW-BTC (QUOTE-BASE 순서)."""
        return f"{symbol.quote}-{symbol.base}"

    async def _get_market_json(self, path: str, native_symbol: str) -> Any:
        """마켓 코드를 넘기는 조회 API 공통 호출부.

        없는 마켓에는 404 + ``{"error": {"name": 404, ...}}`` 가 오므로
        MarketNotFoundError 로 옮겨준다.
        """
        try:
            return await self._get_json(
                f"{self.base_url}{path}", params={"markets": native_symbol}
            )
        except ExchangeAPIError as exc:
            if exc.detail.get("status_code") == 404:
                raise MarketNotFoundError(
                    f"{self.name}에 {native_symbol} 마켓이 존재하지 않습니다.",
                    detail={"exchange": self.id, "native_symbol": native_symbol},
                ) from exc
            raise

    async def _request_orderbook(
        self, native_symbol: str, depth: int, market_type: MarketType
    ) -> Any:
        return await self._get_market_json(self.ORDERBOOK_PATH, native_symbol)

    async def _request_ticker(self, native_symbol: str, market_type: MarketType) -> Any:
        return await self._get_market_json(self.TICKER_PATH, native_symbol)

    # ------------------------------------------------------------------
    # 파싱
    # ------------------------------------------------------------------

    def _levels(
        self, units: list[dict], depth: int, *, is_bid: bool
    ) -> list[OrderBookLevel]:
        """orderbook_units 에서 한쪽 호가만 뽑는다.

        업비트는 하나의 unit 안에 같은 단계의 bid/ask 를 함께 담아 내려주고,
        bid 는 내림차순 · ask 는 오름차순으로 이미 정렬되어 있다.
        (빗썸과 달리 잔량 0 인 유령 호가는 나오지 않는다 — 실측 확인)
        """
        price_key = "bid_price" if is_bid else "ask_price"
        size_key = "bid_size" if is_bid else "ask_size"

        levels = []
        for unit in units:
            levels.append(
                OrderBookLevel(price=float(unit[price_key]), size=float(unit[size_key]))
            )
            if len(levels) >= depth:
                break
        return levels

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
                f"{self.name}에 {native_symbol} 마켓이 존재하지 않습니다.",
                detail={"exchange": self.id, "native_symbol": native_symbol},
            )

        book = raw[0]
        units = book.get("orderbook_units")
        if not units:
            raise ExchangeAPIError(
                f"{self.name} 호가 응답에 orderbook_units 가 없습니다: {native_symbol}",
                detail={"exchange": self.id, "native_symbol": native_symbol},
            )

        return OrderBook(
            exchange=self.id,
            symbol=str(symbol),
            native_symbol=native_symbol,
            market_type=market_type,
            base=symbol.base,
            quote=symbol.quote,
            bids=self._levels(units, depth, is_bid=True),
            asks=self._levels(units, depth, is_bid=False),
            timestamp=int(book.get("timestamp", 0)),
            latency_ms=round(latency_ms, 2),
        )

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
                f"{self.name}에 {native_symbol} 마켓이 존재하지 않습니다.",
                detail={"exchange": self.id, "native_symbol": native_symbol},
            )

        t = raw[0]
        # trade_price / trade_timestamp 는 진짜 마지막 체결값이다 (업비트 실측 검증).
        # 함께 오는 opening_price / high_price / acc_trade_* 등 기간 요약은 쓰지 않는다 —
        # 집계 구간이 거래소마다 달라 비교가 성립하지 않는다.
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

    # ------------------------------------------------------------------
    # 전종목 일괄 조회
    # ------------------------------------------------------------------

    async def _list_markets(self, quote: str) -> list[str]:
        """해당 결제 통화의 마켓 코드 목록. TTL 캐시가 적용된다."""
        cached = self._markets_cache.get(quote)
        now = time.monotonic()
        if cached is not None and now - cached[1] < self.MARKETS_TTL:
            return cached[0]

        data = await self._get_json(f"{self.base_url}{self.MARKETS_PATH}")
        markets = [
            m["market"]
            for m in data
            if isinstance(m, dict) and str(m.get("market", "")).startswith(f"{quote}-")
        ]

        self._markets_cache[quote] = (markets, now)
        return markets

    def _ensure_quote(self, quote: str) -> str:
        quote = quote.upper()
        if quote not in self.quote_currencies:
            raise UnsupportedMarketError(
                f"{self.name}는 {quote} 마켓을 지원하지 않습니다.",
                detail={"exchange": self.id, "quote": quote},
            )
        return quote

    async def fetch_bulk_quotes(
        self,
        quote: str,
        *,
        need_book: bool,
        market_type: MarketType = MarketType.SPOT,
    ) -> dict[str, BulkQuote]:
        """전종목 시세를 가져온다.

        ``markets`` 파라미터에 쉼표로 여러 마켓을 넘길 수 있어 호출 수가 크게 줄어든다.
        """
        quote = self._ensure_quote(quote)
        markets = await self._list_markets(quote)
        if not markets:
            return {}

        path = self.ORDERBOOK_PATH if need_book else self.TICKER_PATH
        chunks = [
            markets[i : i + self.BULK_CHUNK]
            for i in range(0, len(markets), self.BULK_CHUNK)
        ]
        url = f"{self.base_url}{path}"
        responses = await asyncio.gather(
            *(self._get_json(url, params={"markets": ",".join(c)}) for c in chunks)
        )

        result: dict[str, BulkQuote] = {}
        for rows in responses:
            for row in rows:
                native = row.get("market")
                if not native or not native.startswith(f"{quote}-"):
                    continue
                base = native.split("-", 1)[1]

                if need_book:
                    quote_row = self._bulk_from_book(base, quote, native, row)
                else:
                    quote_row = self._bulk_from_ticker(base, quote, native, row)
                if quote_row is not None:
                    result[base] = quote_row

        return result

    def _bulk_from_book(
        self, base: str, quote: str, native: str, row: dict
    ) -> BulkQuote | None:
        """호가 응답에서 최우선 매수/매도를 뽑는다."""
        units = row.get("orderbook_units") or []
        if not units:
            return None
        top = units[0]

        return BulkQuote(
            base=base,
            quote=quote,
            native_symbol=native,
            bid=float(top["bid_price"]),
            ask=float(top["ask_price"]),
            bid_size=float(top["bid_size"]),
            ask_size=float(top["ask_size"]),
        )

    def _bulk_from_ticker(
        self, base: str, quote: str, native: str, row: dict
    ) -> BulkQuote | None:
        price = row.get("trade_price")
        if price is None:
            return None
        return BulkQuote(
            base=base, quote=quote, native_symbol=native, last=float(price)
        )

    async def fetch_bulk_orderbooks(
        self,
        quote: str,
        *,
        depth: int,
        market_type: MarketType = MarketType.SPOT,
    ) -> dict[str, OrderBook]:
        """전종목 호가창을 **깊이까지** 한 번에 가져온다.

        업비트의 ``/v1/orderbook?markets=...`` 은 마켓당 **30단계 전부**를
        내려준다. 즉 최우선 호가만 쓰는 일괄 조회와 **호출 수가 완전히 같은데**
        깊이까지 얻을 수 있다. 슬리피지 계산에 그대로 쓸 수 있다.
        """
        quote = self._ensure_quote(quote)
        markets = await self._list_markets(quote)
        if not markets:
            return {}

        chunks = [
            markets[i : i + self.BULK_CHUNK]
            for i in range(0, len(markets), self.BULK_CHUNK)
        ]
        url = f"{self.base_url}{self.ORDERBOOK_PATH}"

        started = time.perf_counter()
        responses = await asyncio.gather(
            *(self._get_json(url, params={"markets": ",".join(c)}) for c in chunks)
        )
        latency_ms = (time.perf_counter() - started) * 1000

        result: dict[str, OrderBook] = {}
        for rows in responses:
            for row in rows:
                native = row.get("market")
                units = row.get("orderbook_units")
                if not native or not native.startswith(f"{quote}-") or not units:
                    continue

                base = native.split("-", 1)[1]
                bids = self._levels(units, depth, is_bid=True)
                asks = self._levels(units, depth, is_bid=False)
                if not bids or not asks:
                    continue

                result[base] = OrderBook(
                    exchange=self.id,
                    symbol=f"{base}/{quote}",
                    native_symbol=native,
                    market_type=MarketType.SPOT,
                    base=base,
                    quote=quote,
                    bids=bids,
                    asks=asks,
                    timestamp=int(row.get("timestamp", 0)),
                    latency_ms=round(latency_ms, 2),
                )

        return result
