"""거래소 간 가격 비교 라우터."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.models.comparison import ComparisonResult
from app.models.orderbook import MarketType
from app.services.comparison_service import comparison_service

router = APIRouter(prefix="/compare", tags=["compare"])


@router.get(
    "",
    response_model=ComparisonResult,
    summary="거래소 간 가격 비교",
    description=(
        "여러 거래소의 같은 코인 가격을 하나의 통화로 환산해 비교한다.\n\n"
        "각 거래소의 기본 마켓을 자동으로 선택한다 (업비트 KRW, 바이낸스 USDT). "
        "환율은 업비트 `KRW-USDT` 마켓 중간가를 사용한다.\n\n"
        "`spread` 는 수수료·출금비용·전송시간을 반영하지 않은 이론적 가격차다."
    ),
)
async def compare_prices(
    base: Annotated[
        str, Query(description="비교할 코인 심볼", examples=["BTC"])
    ],
    exchanges: Annotated[
        list[str] | None,
        Query(description="비교할 거래소 ID. 생략하면 등록된 전체 거래소"),
    ] = None,
    common_currency: Annotated[
        str, Query(description="환산 기준 통화 (KRW 또는 USDT)")
    ] = "KRW",
    market_type: Annotated[
        MarketType, Query(description="현물(spot) / 선물(futures)")
    ] = MarketType.SPOT,
) -> ComparisonResult:
    return await comparison_service.compare(
        base,
        exchanges=exchanges,
        common_currency=common_currency,
        market_type=market_type,
    )
