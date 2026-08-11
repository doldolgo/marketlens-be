"""바이낸스(Binance) 커넥터.

원본 엔드포인트
    현물: GET https://api.binance.com/api/v3/depth?symbol=BTCUSDT&limit=10
    선물: GET https://fapi.binance.com/fapi/v1/depth?symbol=BTCUSDT&limit=10
    - 둘 다 인증 불필요 (public market data)
    - limit 허용값: 현물 5/10/20/50/100/500/1000/5000, 선물 5/10/20/50/100/500/1000
    - 응답의 bids/asks 는 ["가격", "수량"] 문자열 배열이다.

현물 depth 응답에는 거래소 타임스탬프가 없어서 수신 시각을 사용하고,
선물 응답에는 ``E`` (이벤트 시각) 필드가 있어 그대로 쓴다.
"""

from __future__ import annotations

import time
from typing import Any, ClassVar

from app.core.config import settings
from app.core.errors import (
    ExchangeAPIError,
    MarketNotFoundError,
    UnsupportedMarketError,
)
from app.models.bulk import BulkQuote
from app.models.orderbook import MarketType, OrderBook, OrderBookLevel
from app.models.symbol import Symbol
from app.models.ticker import Ticker
from app.exchanges.base import BaseExchange


class Binance(BaseExchange):
    id: ClassVar[str] = "binance"
    name: ClassVar[str] = "바이낸스"
    quote_currencies: ClassVar[frozenset[str]] = frozenset(
        {"USDT", "USDC", "BTC", "ETH", "BNB", "FDUSD"}
    )
    default_quote: ClassVar[str] = "USDT"
    supported_market_types: ClassVar[frozenset[MarketType]] = frozenset(
        {MarketType.SPOT, MarketType.FUTURES}
    )

    SPOT_PATH = "/api/v3/depth"
    FUTURES_PATH = "/fapi/v1/depth"
    # 마지막 체결가는 aggTrades 로 가져온다.
    # ticker/24hr 의 closeTime 은 공식 문서상 "End of the ticker interval" 이라
    # 마지막 체결 시각이 아니다. 실측으로도 확인했다: CRDOBUSDT 는 마지막 체결이
    # 62분 전인데 closeTime 은 53초 전(≈현재)이었다.
    # aggTrades 의 T 는 실제 체결 시각이고 p 는 체결가다.
    SPOT_TICKER_PATH = "/api/v3/aggTrades"
    FUTURES_TICKER_PATH = "/fapi/v1/aggTrades"

    #: 바이낸스가 허용하는 limit 값. 요청 depth 를 이 중 하나로 올림한다.
    ALLOWED_LIMITS: ClassVar[tuple[int, ...]] = (5, 10, 20, 50, 100, 500, 1000)

    # 전종목 일괄 조회 — 심볼을 지정하지 않으면 전체가 오고 weight 는 4밖에 안 든다.
    # (단일 심볼 조회가 weight 2 이므로, 2개 이상이면 전체 조회가 오히려 싸다)
    SPOT_BULK_PRICE_PATH = "/api/v3/ticker/price"
    SPOT_BULK_BOOK_PATH = "/api/v3/ticker/bookTicker"
    FUTURES_BULK_PRICE_PATH = "/fapi/v1/ticker/price"
    FUTURES_BULK_BOOK_PATH = "/fapi/v1/ticker/bookTicker"

    supports_bulk: ClassVar[bool] = True

    def to_native_symbol(self, symbol: Symbol, market_type: MarketType) -> str:
        """BTC/USDT -> BTCUSDT (바이낸스는 구분자 없는 BASE+QUOTE)."""
        return f"{symbol.base}{symbol.quote}"

    def _normalize_limit(self, depth: int) -> int:
        """요청 depth 를 바이낸스가 허용하는 limit 값으로 올림한다."""
        for allowed in self.ALLOWED_LIMITS:
            if depth <= allowed:
                return allowed
        return self.ALLOWED_LIMITS[-1]

    async def _request_orderbook(
        self, native_symbol: str, depth: int, market_type: MarketType
    ) -> Any:
        if market_type is MarketType.FUTURES:
            url = f"{settings.binance_futures_base_url}{self.FUTURES_PATH}"
        else:
            url = f"{settings.binance_spot_base_url}{self.SPOT_PATH}"

        params = {"symbol": native_symbol, "limit": self._normalize_limit(depth)}
        return await self._get_symbol_json(url, native_symbol, params)

    async def _get_symbol_json(
        self, url: str, native_symbol: str, params: dict[str, Any]
    ) -> Any:
        """심볼 기반 조회 API 공통 호출부.

        바이낸스는 없는 심볼에 400 + ``{"code": -1121, "msg": "Invalid symbol."}`` 를
        반환하므로 이를 MarketNotFoundError 로 옮겨준다.
        """
        try:
            return await self._get_json(url, params=params)
        except ExchangeAPIError as exc:
            if "-1121" in str(exc.detail.get("body", "")):
                raise MarketNotFoundError(
                    f"바이낸스에 {native_symbol} 마켓이 존재하지 않습니다.",
                    detail={"exchange": self.id, "native_symbol": native_symbol},
                ) from exc
            raise

    async def _request_ticker(self, native_symbol: str, market_type: MarketType) -> Any:
        if market_type is MarketType.FUTURES:
            url = f"{settings.binance_futures_base_url}{self.FUTURES_TICKER_PATH}"
        else:
            url = f"{settings.binance_spot_base_url}{self.SPOT_TICKER_PATH}"

        return await self._get_symbol_json(
            url, native_symbol, {"symbol": native_symbol, "limit": 1}
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
        # aggTrades 는 최신순이 아니라 오래된 순이므로, limit=1 로 받은 마지막 원소를 쓴다.
        if not isinstance(raw, list) or not raw:
            raise MarketNotFoundError(
                f"바이낸스 {native_symbol} 에 체결 내역이 없습니다.",
                detail={"exchange": self.id, "native_symbol": native_symbol},
            )

        trade = raw[-1]
        if "p" not in trade or "T" not in trade:
            raise ExchangeAPIError(
                f"바이낸스 체결 응답 형식이 올바르지 않습니다: {native_symbol}",
                detail={"exchange": self.id, "native_symbol": native_symbol},
            )

        return Ticker(
            exchange=self.id,
            symbol=str(symbol),
            native_symbol=native_symbol,
            market_type=market_type,
            base=symbol.base,
            quote=symbol.quote,
            last_price=float(trade["p"]),  # 체결 가격
            timestamp=int(trade["T"]),  # 체결 시각 (진짜 마지막 체결 시각)
            latency_ms=round(latency_ms, 2),
        )

    # ------------------------------------------------------------------
    # 전종목 일괄 조회
    # ------------------------------------------------------------------

    async def fetch_bulk_quotes(
        self,
        quote: str,
        *,
        need_book: bool,
        market_type: MarketType = MarketType.SPOT,
    ) -> dict[str, BulkQuote]:
        """바이낸스 전종목 시세를 가져온다.

        심볼 파라미터 없이 호출하면 전체(현물 약 3,700개)가 오고 **weight 는 4**다.
        단일 조회가 weight 2 이므로 2종목만 넘어가도 전체 조회가 더 싸다.

        바이낸스 심볼에는 구분자가 없어서(``BTCUSDT``) 접미사로 결제 통화를 판별한다.
        """
        quote = quote.upper()
        if quote not in self.quote_currencies:
            raise UnsupportedMarketError(
                f"바이낸스는 {quote} 마켓을 지원하지 않습니다.",
                detail={"exchange": self.id, "quote": quote},
            )

        if market_type is MarketType.FUTURES:
            base_url = settings.binance_futures_base_url
            path = (
                self.FUTURES_BULK_BOOK_PATH
                if need_book
                else self.FUTURES_BULK_PRICE_PATH
            )
        else:
            base_url = settings.binance_spot_base_url
            path = self.SPOT_BULK_BOOK_PATH if need_book else self.SPOT_BULK_PRICE_PATH

        rows = await self._get_json(f"{base_url}{path}")
        if not isinstance(rows, list):
            raise ExchangeAPIError(
                "바이낸스 전종목 응답 형식이 올바르지 않습니다.",
                detail={"exchange": self.id, "path": path},
            )

        result: dict[str, BulkQuote] = {}
        for row in rows:
            native = row.get("symbol")
            if not native or not native.endswith(quote):
                continue
            base = native[: -len(quote)]
            if not base:
                continue

            if need_book:
                bid, ask = row.get("bidPrice"), row.get("askPrice")
                if bid is None or ask is None:
                    continue
                bid_f, ask_f = float(bid), float(ask)
                # 거래가 없는 심볼은 호가가 0 으로 온다.
                if bid_f <= 0 or ask_f <= 0:
                    continue
                result[base] = BulkQuote(
                    base=base,
                    quote=quote,
                    native_symbol=native,
                    bid=bid_f,
                    ask=ask_f,
                    bid_size=float(row.get("bidQty") or 0.0),
                    ask_size=float(row.get("askQty") or 0.0),
                )
            else:
                price = row.get("price")
                if price is None:
                    continue
                price_f = float(price)
                if price_f <= 0:
                    continue
                result[base] = BulkQuote(
                    base=base, quote=quote, native_symbol=native, last=price_f
                )

        return result

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
        if not isinstance(raw, dict) or "bids" not in raw or "asks" not in raw:
            raise ExchangeAPIError(
                f"바이낸스 호가 응답 형식이 올바르지 않습니다: {native_symbol}",
                detail={"exchange": self.id, "native_symbol": native_symbol},
            )

        bids = [
            OrderBookLevel(price=float(price), size=float(size))
            for price, size in raw["bids"][:depth]
        ]
        asks = [
            OrderBookLevel(price=float(price), size=float(size))
            for price, size in raw["asks"][:depth]
        ]

        # 선물은 E(이벤트 시각), 현물은 타임스탬프가 없으므로 수신 시각으로 대체한다.
        timestamp = int(raw.get("E") or raw.get("T") or time.time() * 1000)

        return OrderBook(
            exchange=self.id,
            symbol=str(symbol),
            native_symbol=native_symbol,
            market_type=market_type,
            base=symbol.base,
            quote=symbol.quote,
            bids=bids,
            asks=asks,
            timestamp=timestamp,
            latency_ms=round(latency_ms, 2),
        )
