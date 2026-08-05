"""시세 조회 서비스 — 호가창과 티커.

단일 거래소 조회와 다중 거래소 동시 조회를 모두 제공한다.
동시 조회의 부분 실패 처리는 :mod:`app.services.fanout` 이 담당한다.
"""

from __future__ import annotations

from app.exchanges.registry import get_exchange
from app.models.orderbook import MarketType, OrderBook
from app.models.symbol import Symbol
from app.models.ticker import Ticker
from app.services.fanout import FanOutFailure, Target, fan_out


class MarketDataService:
    """호가창 / 티커 조회."""

    # ------------------------------------------------------------------
    # 호가창
    # ------------------------------------------------------------------

    async def fetch_orderbook(
        self,
        exchange_id: str,
        symbol: Symbol,
        *,
        depth: int = 10,
        market_type: MarketType = MarketType.SPOT,
    ) -> OrderBook:
        """거래소 한 곳의 호가를 조회한다."""
        exchange = get_exchange(exchange_id)
        return await exchange.fetch_orderbook(symbol, depth=depth, market_type=market_type)

    async def fetch_orderbooks(
        self,
        targets: list[Target],
        *,
        depth: int = 10,
    ) -> tuple[list[OrderBook], list[FanOutFailure]]:
        """여러 거래소의 호가를 동시에 조회한다."""
        return await fan_out(
            targets,
            lambda t: self.fetch_orderbook(t[0], t[1], depth=depth, market_type=t[2]),
        )

    # ------------------------------------------------------------------
    # 티커 (현재가)
    # ------------------------------------------------------------------

    async def fetch_ticker(
        self,
        exchange_id: str,
        symbol: Symbol,
        *,
        market_type: MarketType = MarketType.SPOT,
    ) -> Ticker:
        """거래소 한 곳의 현재가를 조회한다."""
        exchange = get_exchange(exchange_id)
        return await exchange.fetch_ticker(symbol, market_type=market_type)

    async def fetch_tickers(
        self,
        targets: list[Target],
    ) -> tuple[list[Ticker], list[FanOutFailure]]:
        """여러 거래소의 현재가를 동시에 조회한다."""
        return await fan_out(targets, lambda t: self.fetch_ticker(t[0], t[1], market_type=t[2]))


market_data_service = MarketDataService()
