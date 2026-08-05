"""호가 조회 라우터."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.core.config import settings
from app.models.orderbook import MarketType, OrderBook
from app.models.symbol import Symbol
from app.services.market_data_service import market_data_service

router = APIRouter(prefix="/orderbook", tags=["orderbook"])

SymbolQuery = Annotated[
    str,
    Query(description="통일 심볼. BASE/QUOTE 형식 (예: BTC/KRW, BTC/USDT)", examples=["BTC/KRW"]),
]
DepthQuery = Annotated[int, Query(ge=1, le=100, description="조회할 호가 단계 수")]
MarketTypeQuery = Annotated[MarketType, Query(description="현물(spot) / 선물(futures)")]


@router.get(
    "/{exchange_id}",
    response_model=OrderBook,
    summary="단일 거래소 호가 조회",
    description=(
        "지정한 거래소의 호가창을 조회한다.\n\n"
        "- `upbit` → `GET https://api.upbit.com/v1/orderbook`\n"
        "- `binance` (spot) → `GET https://api.binance.com/api/v3/depth`\n"
        "- `binance` (futures) → `GET https://fapi.binance.com/fapi/v1/depth`\n\n"
        "세 엔드포인트 모두 인증이 필요 없는 public API 다."
    ),
)
async def get_orderbook(
    exchange_id: str,
    symbol: SymbolQuery,
    depth: DepthQuery = settings.default_orderbook_depth,
    market_type: MarketTypeQuery = MarketType.SPOT,
) -> OrderBook:
    return await market_data_service.fetch_orderbook(
        exchange_id,
        Symbol.parse(symbol),
        depth=depth,
        market_type=market_type,
    )
