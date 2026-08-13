"""환율 라우터 — DB 에 저장된 통일 환율(USD/KRW) 조회.

거래소를 직접 호출하지 않는다. ``POST /refresh`` (또는 ``POST /history/sync``)
가 ``fx_rate`` 테이블에 저장해둔 **하나은행 고시 매매기준율**을 읽어서
반환할 뿐이다.

예전에는 국내 거래소별 KRW-USDT 마켓 시세를 환율로 썼지만, 그 값에는
테더 프리미엄이 섞여 있어 "은행 환율 기준 김프" 와 어긋난다. 지금은 모든
계산이 이 은행 고시 환율 하나로 통일됐다. 과거 환율이 필요하면
``GET /history/fx`` 를 쓴다.

테스트 예시:
    http://3.34.104.16:8000/rate        (배포 서버)
    http://localhost:8000/rate          (로컬 실행 시)
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import repository
from app.db.database import get_session

router = APIRouter(prefix="/rate", tags=["rate"])


def _epoch_ms(dt: datetime | None) -> int | None:
    return int(dt.timestamp() * 1000) if dt is not None else None


class RateResponse(BaseModel):
    """저장된 통일 환율 (하나은행 고시 USD/KRW 매매기준율)."""

    rate: float = Field(..., description="USD 1달러당 원화 (매매기준율)")
    source: str = Field(
        "hana", description="환율 출처 — 하나은행 고시환율로 고정"
    )
    source_time: int = Field(
        ..., description="은행이 이 환율을 고시한 시각 (epoch 초)"
    )
    round_no: int = Field(..., description="당일 고시 회차")
    updated_at: int | None = Field(
        ..., description="이 환율을 DB 에 저장한 시각 (epoch ms) — 데이터 신선도 기준"
    )
    fetched_at: int = Field(..., description="이 응답을 만든 시각 (epoch ms)")


@router.get(
    "",
    response_model=RateResponse,
    summary="USD/KRW 환율 조회 (DB 저장값)",
    description=(
        "원화 환산에 쓰는 **통일 환율**을 DB 에서 조회한다.\n\n"
        "값은 **하나은행 고시 USD/KRW 매매기준율**이다. 은행은 하루 "
        "1,300~2,000회 고시하며, `POST /refresh` 와 `POST /history/sync` 가 "
        "최신 고시를 저장한다. `source_time` 이 은행 고시 시각, `updated_at` "
        "이 저장 시각이다.\n\n"
        "### 예전과 달라진 점\n\n"
        "이전 버전은 국내 거래소별 `KRW-USDT` 마켓 시세를 환율로 썼다. "
        "그 값에는 **테더 프리미엄**이 섞여 있어 거래소마다 다르고, 은행 환율 "
        "기준의 김프와 어긋난다. 지금은 은행 고시 환율 하나로 통일됐고 "
        "`exchange` 파라미터도 없어졌다.\n\n"
        "과거 환율의 변동 이력은 `GET /history/fx` 로 조회한다."
    ),
)
async def get_rate(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RateResponse:
    row = await repository.require_fx_rate(session)
    return RateResponse(
        rate=row.rate,
        source_time=row.source_time,
        round_no=row.round_no,
        updated_at=_epoch_ms(row.updated_at),
        fetched_at=int(time.time() * 1000),
    )
