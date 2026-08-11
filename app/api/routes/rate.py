"""환율 라우터 — DB 에 저장된 KRW-USDT 환율 조회.

거래소를 직접 호출하지 않는다. ``POST /refresh`` 가 ``krw_rates`` 테이블에
저장해둔 국내 거래소별 환율(마지막 체결가)을 읽어서 반환할 뿐이다.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import MarketDataNotFoundError
from app.db import repository
from app.db.database import get_session
from app.exchanges.registry import domestic_exchange_ids, get_exchange

router = APIRouter(prefix="/rate", tags=["rate"])


def _epoch_ms(dt: datetime | None) -> int | None:
    return int(dt.timestamp() * 1000) if dt is not None else None


class RateEntry(BaseModel):
    """국내 거래소 하나의 USDT/KRW 환율 (DB 저장값)."""

    exchange: str = Field(..., description="환율을 뽑은 국내 거래소 ID")
    rate: float = Field(..., description="USDT 1개의 원화 가격 (마지막 체결가)")
    updated_at: int | None = Field(
        ..., description="이 환율을 DB 에 저장한 시각 (epoch ms) — 데이터 신선도 기준"
    )


class RateResponse(BaseModel):
    """저장된 USDT/KRW 환율 목록."""

    rates: list[RateEntry] = Field(..., description="국내 거래소별 환율")
    fetched_at: int = Field(..., description="이 응답을 만든 시각 (epoch ms)")


@router.get(
    "",
    response_model=RateResponse,
    summary="USDT/KRW 환율 조회 (DB 저장값)",
    description=(
        "원화 환산에 쓰는 환율을 **DB 에서** 조회한다.\n\n"
        "거래소를 직접 호출하지 않는다. 반환값은 **저장 시점의 국내 거래소 "
        "`KRW-USDT` 마켓 마지막 체결가**이며, `POST /refresh` 로 갱신된다. "
        "각 항목의 `updated_at` 으로 얼마나 오래된 값인지 알 수 있다.\n\n"
        "### 어떤 환율인가\n\n"
        "**국내 거래소의 `KRW-USDT` 마켓 시세**다. 은행 고시 USD/KRW 가 아니다.\n\n"
        "실제 국내 시장에서 거래되는 테더 가격이므로 "
        '"원화로 사서 해외에서 팔면 실제로 얼마가 남는가" 에 훨씬 가깝다. '
        "은행 환율과의 차이가 곧 **테더 프리미엄**이다.\n\n"
        "### 거래소마다 값이 다르다\n\n"
        "빗썸 `KRW-USDT` 와 업비트 `KRW-USDT` 는 서로 다른 시장이라 값이 다르다. "
        "그래서 국내 거래소별로 한 행씩 저장하고, 원화 환산이 필요한 조회 API 들은 "
        "해당 국내 거래소의 환율을 쓰되 없으면 기준 거래소"
        f"(`{settings.krw_reference_exchange}`) 환율로 폴백한다.\n\n"
        "`exchange` 를 지정하면 그 거래소 것만, 생략하면 저장된 전체를 반환한다. "
        "지정한 거래소의 환율이 DB 에 없으면 404."
    ),
)
async def get_rates(
    session: Annotated[AsyncSession, Depends(get_session)],
    exchange: Annotated[
        str | None,
        Query(
            description=(
                "환율을 조회할 **국내 거래소** ID. 생략하면 저장된 전체. "
                f"선택 가능: {', '.join(domestic_exchange_ids())}"
            ),
            examples=["upbit"],
        ),
    ] = None,
) -> RateResponse:
    if exchange is not None:
        # 등록되지 않은 거래소 ID 는 다른 엔드포인트와 같은 에러로 걸러낸다
        # (unsupported_exchange 404 — DB 에 없는 것과 구분된다).
        rows = [await repository.require_krw_rate(session, get_exchange(exchange).id)]
    else:
        rows = await repository.get_krw_rates(session)
        if not rows:
            raise MarketDataNotFoundError(
                "DB 에 KRW-USDT 환율이 없습니다. 먼저 POST /refresh 로 수집하세요.",
            )
    return RateResponse(
        rates=[
            RateEntry(
                exchange=r.exchange,
                rate=r.rate,
                updated_at=_epoch_ms(r.updated_at),
            )
            for r in sorted(rows, key=lambda r: r.exchange)
        ],
        fetched_at=int(time.time() * 1000),
    )
