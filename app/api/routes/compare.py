"""거래소 간 가격 비교 라우터 — DB 스냅샷 기반."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_session
from app.models.comparison import ComparisonResult
from app.services.comparison_service import comparison_service

router = APIRouter(prefix="/compare", tags=["compare"])


@router.get(
    "",
    response_model=ComparisonResult,
    summary="거래소 간 가격 비교",
    description=(
        "여러 거래소의 같은 코인 가격(마지막 체결가)을 하나의 통화로 환산해 "
        "비교한다.\n\n"
        "거래소를 직접 호출하지 않고 **DB 스냅샷만 읽는다.** 데이터가 오래됐으면 "
        "`data_oldest_at` 으로 알 수 있고, `POST /refresh` 로 갱신한다.\n\n"
        "환율은 DB 의 `krw_rates` 를 사용한다 — KRW 환산은 기준 국내 거래소(업비트) "
        "환율을 곱하고, USDT 환산은 그 국내 거래소 자기 환율로 나눈다 "
        "(없으면 기준 환율).\n\n"
        "`spread` 는 수수료·출금비용·전송시간을 반영하지 않은 이론적 가격차다."
    ),
)
async def compare_prices(
    session: Annotated[AsyncSession, Depends(get_session)],
    sym: Annotated[str, Query(description="비교할 코인 심볼", examples=["BTC"])],
    exchanges: Annotated[
        list[str] | None,
        Query(description="비교할 거래소 ID. 생략하면 스냅샷이 있는 전체 거래소"),
    ] = None,
    common_currency: Annotated[
        str, Query(description="환산 기준 통화 (KRW 또는 USDT)")
    ] = "KRW",
) -> ComparisonResult:
    return await comparison_service.compare(session, sym,
        exchanges=exchanges,
        common_currency=common_currency,
    )
