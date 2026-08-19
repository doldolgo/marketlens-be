"""환율 라우터 — DB 에 저장된 KRW-USDT 환율 조회 (국내 거래소별).

거래소를 직접 호출하지 않는다. ``POST /refresh`` 가 ``usdkrw_rate`` 테이블에
저장해둔 **국내 거래소별 KRW-USDT 최우선 호가**를 읽어서 반환할 뿐이다.

원화 ↔ 달러 전환은 은행이 아니라 국내 거래소의 USDT 마켓에서 일어난다. 그래서
환율도 그 마켓의 호가로 잡고, **방향별로 다른 값**을 쓴다 — 김프는 ask(원화로
USDT 매수), 역프는 bid(USDT 를 원화로 매도). 테더 프리미엄은 노이즈가 아니라
실제로 치르는 비용이며, 거래소마다 다르므로 행도 거래소마다 따로 둔다.

과거 환율이 필요하면 ``GET /history/premium``(김프 기록)을 쓴다.

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

from app.core.errors import MarketDataNotFoundError
from app.db import repository
from app.db.database import get_session

router = APIRouter(prefix="/rate", tags=["rate"])


def _epoch_ms(dt: datetime | None) -> int | None:
    return int(dt.timestamp() * 1000) if dt is not None else None


class ExchangeRate(BaseModel):
    """한 국내 거래소의 KRW-USDT 환율."""

    exchange: str = Field(..., description="국내 거래소 ID (upbit / bithumb)")
    ask: float = Field(
        ...,
        description="최우선 매도호가 — 원화로 USDT 를 살 때 체결. **김프 계산에 쓴다**",
    )
    bid: float = Field(
        ...,
        description="최우선 매수호가 — USDT 를 원화로 팔 때 체결. **역프 계산에 쓴다**",
    )
    updated_at: int | None = Field(
        ..., description="이 환율을 DB 에 저장한 시각 (epoch ms) — 데이터 신선도 기준"
    )


class RateResponse(BaseModel):
    """저장된 KRW-USDT 환율 (국내 거래소별)."""

    rates: list[ExchangeRate] = Field(
        default_factory=list, description="국내 거래소별 환율. 거래소 ID 순 정렬"
    )
    fetched_at: int = Field(..., description="이 응답을 만든 시각 (epoch ms)")


@router.get(
    "",
    response_model=RateResponse,
    summary="KRW-USDT 환율 조회 (DB 저장값, 거래소별)",
    description=(
        "원화 환산에 쓰는 환율을 DB 에서 조회한다.\n\n"
        "값은 **국내 거래소 KRW-USDT 마켓의 최우선 호가**다. `POST /refresh` 가 "
        "국내 KRW 전종목 호가를 받을 때 함께 들어오는 값이라 별도 조회가 없다.\n\n"
        "### 왜 ask 와 bid 가 따로인가\n\n"
        "환전도 체결되는 쪽 호가에서 일어난다. 김프(해외 매수 → 국내 매도)는 "
        "원화로 USDT 를 **사서** 시작하므로 `ask`, 역프(국내 매수 → 해외 매도)는 "
        "받은 USDT 를 원화로 **팔아서** 끝나므로 `bid` 를 쓴다. "
        "`ask > bid` 이므로 양방향 모두 스프레드만큼 보수적으로 나온다.\n\n"
        "### 왜 거래소별인가\n\n"
        "테더 프리미엄이 거래소마다 다르다. 업비트 김프는 업비트 USDT 호가로, "
        "빗썸 김프는 빗썸 USDT 호가로 계산해야 실제 실행 수익률에 맞는다.\n\n"
        "과거 김프/역프 기록은 `GET /history/premium` 으로 조회한다."
    ),
)
async def get_rate(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RateResponse:
    rows = await repository.get_usdkrw_rates(session)
    if not rows:
        raise MarketDataNotFoundError(
            "DB 에 KRW-USDT 환율이 없습니다. POST /refresh 로 수집했는지 확인하세요.",
        )
    return RateResponse(
        rates=[
            ExchangeRate(
                exchange=row.exchange,
                ask=row.ask,
                bid=row.bid,
                updated_at=_epoch_ms(row.updated_at),
            )
            for _, row in sorted(rows.items())
        ],
        fetched_at=int(time.time() * 1000),
    )
