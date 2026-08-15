"""기록/통계 라우터 — 김프/역프 아카이브와 플랫폼 상태 조회.

두 엔드포인트다.

    GET /history/premium — 김프/역프 기록 (주/월 단위 로그, 기록/통계 창)
    GET /history/status  — 플랫폼별 수신 상태·입출금 실패율

데이터 출처
    ``premium_archive`` — POST /refresh 가 매 회차 스냅샷에서 계산해 쌓고,
    과거 구간은 scripts/bulk_archive.py 가 거래소 캔들로 채운 기록.
    ``platform_status`` — refresh 가 플랫폼당 한 행으로 유지하는 카운터.

응답 형식의 핵심: **DB 는 절대 시각(epoch 초)으로 저장하고, 응답은 상대
시각으로 준다.** 각 기록은 "직전 기록에서 몇 초 뒤에(dt) 김프가 얼마(fwd),
역프가 얼마(rev)였나"다. first_ts 에 dt 를 누적하면 절대 타임라인이 복원된다.

테스트 예시 (배포 서버 기준 — 로컬이면 http://localhost:8000 으로 바꾼다):
    이번 주 김프 기록 (최근 20건):
      http://3.34.104.16:8000/history/premium?base=BTC&unit=week&limit=20
    특정 월 (8월 전체):
      http://3.34.104.16:8000/history/premium?base=BTC&unit=month&date=2026-08-14
    플랫폼 상태:
      http://3.34.104.16:8000/history/status
"""

from __future__ import annotations

import time
from datetime import date, datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InvalidRequestError, MarketDataNotFoundError
from app.db import repository
from app.db.database import get_session
from app.db.models import PremiumArchive
from app.history import service as history_service

router = APIRouter(prefix="/history", tags=["history"])

#: 한 응답에 담는 기록 수 상한. 한 달치 기록은 수십만 건이 될 수 있어
#: offset/limit 페이지네이션을 강제한다 (총 개수는 count 로 알 수 있다).
MAX_EVENTS = 50_000
DEFAULT_EVENTS = 10_000


class PremiumEvent(BaseModel):
    """김프/역프 기록 한 건 — 직전 기록에서 몇 초 뒤의 값인가."""

    dt: int = Field(
        ...,
        description=(
            "직전 기록으로부터의 경과 초. 첫 기록은 0. "
            "(절대 시각 = first_ts + 자기까지의 dt 누적)"
        ),
    )
    fwd: float = Field(..., description="김프 % — 해외 매수 → 국내 매도 수익률")
    rev: float = Field(..., description="역프 % — 국내 매수 → 해외 매도 수익률")


class PremiumSummary(BaseModel):
    """조회 구간의 김프 요약."""

    first_fwd: float = Field(..., description="구간 첫 김프 %")
    last_fwd: float = Field(..., description="구간 마지막 김프 %")
    min_fwd: float = Field(..., description="구간 최저 김프 %")
    max_fwd: float = Field(..., description="구간 최고 김프 %")


class PremiumHistoryResponse(BaseModel):
    """김프/역프 기록 응답 (기록/통계 창)."""

    dom: str = Field(..., description="국내 거래소 ID")
    fx: str = Field(..., description="해외 거래소 ID")
    base: str = Field(..., description="코인 심볼")

    unit: Literal["week", "month"] = Field(..., description="조회 구간 단위")
    start: str = Field(..., description="구간 시작 (UTC, ISO8601)")
    end: str = Field(..., description="구간 끝 (UTC, ISO8601, exclusive)")

    first_ts: int = Field(
        ...,
        description=(
            "구간 내 첫 기록의 절대 시각 (epoch 초). "
            "이 값에 dt 를 누적하면 절대 타임라인이 복원된다. "
            "(기록이 없는 구간은 404 로 응답한다)"
        ),
    )
    count: int = Field(..., description="구간 전체 기록 수")
    offset: int = Field(..., description="이번 페이지 시작 인덱스")
    returned: int = Field(..., description="이번 페이지에 담긴 기록 수")
    has_more: bool = Field(..., description="다음 페이지 존재 여부")

    summary: PremiumSummary = Field(..., description="구간 요약")
    events: list[PremiumEvent] = Field(..., description="기록 (시각 순)")

    fetched_at: int = Field(..., description="응답 생성 시각 (epoch ms)")


class PlatformStatusEntry(BaseModel):
    """플랫폼 하나의 수신 상태."""

    exchange: str = Field(..., description="플랫폼(거래소) ID")
    last_received_ts: int = Field(..., description="마지막 수신 시각 (epoch 초)")
    spot_market_count: int = Field(..., description="상장 현물 마켓 수")
    futures_market_count: int = Field(..., description="상장 선물 마켓 수 (없으면 0)")
    dw_fail_count: int = Field(
        ..., description="입출금 불가가 관측된 업데이트 횟수"
    )
    update_count: int = Field(..., description="전체 업데이트 횟수")
    dw_fail_rate: float = Field(
        ..., description="입출금 실패율 = dw_fail_count / update_count (0~1)"
    )


class PlatformStatusResponse(BaseModel):
    """플랫폼별 수신 상태 목록."""

    platforms: list[PlatformStatusEntry] = Field(..., description="플랫폼별 상태")
    fetched_at: int = Field(..., description="응답 생성 시각 (epoch ms)")


def _build_page(
    fetched: list[PremiumArchive], offset: int
) -> list[PremiumEvent]:
    """페이지 행들을 상대 시각(dt) 이벤트로 변환한다.

    ``offset > 0`` 이면 fetched[0] 은 **직전 행(기준점)** 이다 — 페이지 첫
    항목의 dt 를 계산하는 데만 쓰고 응답에는 싣지 않는다. dt 는 항상 전체
    열 기준의 직전 기록 대비라, 페이지를 이어 붙이면 완전한 로그가 된다.
    """
    out: list[PremiumEvent] = []
    if offset > 0:
        anchor, rows = fetched[0], fetched[1:]
        prev_ts = anchor.ts
        for row in rows:
            out.append(PremiumEvent(dt=row.ts - prev_ts, fwd=row.fwd, rev=row.rev))
            prev_ts = row.ts
    else:
        prev_ts: int | None = None
        for row in fetched:
            dt = 0 if prev_ts is None else row.ts - prev_ts
            out.append(PremiumEvent(dt=dt, fwd=row.fwd, rev=row.rev))
            prev_ts = row.ts
    return out


def _parse_anchor(anchor: str | None) -> date:
    """date 쿼리 파라미터(YYYY-MM-DD)를 파싱한다. 생략하면 오늘(UTC)."""
    if anchor is None:
        return datetime.now(tz=timezone.utc).date()
    try:
        return date.fromisoformat(anchor)
    except ValueError:
        raise InvalidRequestError(
            f"date 는 YYYY-MM-DD 형식이어야 합니다: {anchor}"
        ) from None


@router.get(
    "/premium",
    response_model=PremiumHistoryResponse,
    summary="김프/역프 기록 조회 (주/월 단위)",
    description=(
        "한 코인의 김프/역프가 **언제, 얼마 만에, 얼마로** 바뀌었는지의 기록을 "
        "주/월 단위 구간으로 반환한다 — 기록/통계 창의 데이터 소스.\n\n"
        "기록은 두 경로로 쌓인 것이다: `POST /refresh` 가 매 회차 스냅샷의 "
        "체결측 호가로 계산한 실시간 기록(분 단위)과, 대량 업데이트 스크립트가 "
        "거래소 캔들(초 단위)로 채운 과거 기록.\n\n"
        "DB 에는 절대 시각(epoch 초)으로 저장돼 있고, 응답의 각 기록은 직전 "
        "기록과의 **시간 차(dt 초)** 로 표현된다. `first_ts` 에 dt 를 누적하면 "
        "절대 타임라인이 복원된다.\n\n"
        "구간 전체 기록이 수십만 건일 수 있어 `offset`/`limit` 로 나눠 받는다."
    ),
)
async def premium_history(
    session: Annotated[AsyncSession, Depends(get_session)],
    base: Annotated[str, Query(description="코인 심볼", examples=["BTC"])],
    unit: Annotated[
        Literal["week", "month"],
        Query(description="조회 구간 단위 — week(ISO 주) 또는 month(달력 월)"),
    ],
    dom: Annotated[
        Literal["upbit", "bithumb"],
        Query(description="국내 거래소 ID"),
    ] = "upbit",
    fx: Annotated[
        Literal["binance"],
        Query(description="해외 거래소 ID"),
    ] = "binance",
    date_: Annotated[
        str | None,
        Query(
            alias="date",
            description=(
                "구간을 고를 기준 날짜 (YYYY-MM-DD). 그 날짜가 **속한** 주/월이 "
                "조회된다. 생략하면 오늘(UTC)."
            ),
            examples=["2026-08-14"],
        ),
    ] = None,
    offset: Annotated[int, Query(ge=0, description="기록 페이지 시작 인덱스")] = 0,
    limit: Annotated[
        int,
        Query(ge=1, le=MAX_EVENTS, description=f"페이지당 기록 수 (최대 {MAX_EVENTS:,})"),
    ] = DEFAULT_EVENTS,
) -> PremiumHistoryResponse:
    start_ts, end_ts = history_service.period_range(unit, _parse_anchor(date_))
    # 구간 전체를 메모리에 올리지 않는다 — 요약은 SQL 집계, 페이지는 LIMIT/OFFSET.
    stats = await repository.get_premium_stats(
        session, dom, fx, base.upper(), start_ts, end_ts
    )
    if stats is None:
        raise MarketDataNotFoundError(
            f"{dom}×{fx} {base.upper()} 의 해당 구간 김프 기록이 없습니다. "
            "refresh 가 돌고 있는지, 과거 구간은 bulk_archive 스크립트를 "
            "실행했는지 확인하세요.",
            detail={"dom": dom, "fx": fx, "base": base.upper(), "unit": unit},
        )

    fetched = await repository.get_premium_page(
        session, dom, fx, base.upper(), start_ts, end_ts, offset, limit
    )
    page = _build_page(fetched, offset)
    return PremiumHistoryResponse(
        dom=dom,
        fx=fx,
        base=base.upper(),
        unit=unit,
        start=datetime.fromtimestamp(start_ts, tz=timezone.utc).isoformat(),
        end=datetime.fromtimestamp(end_ts, tz=timezone.utc).isoformat(),
        first_ts=stats["first_ts"],
        count=stats["count"],
        offset=offset,
        returned=len(page),
        has_more=offset + limit < stats["count"],
        summary=PremiumSummary(
            first_fwd=stats["first_fwd"],
            last_fwd=stats["last_fwd"],
            min_fwd=stats["min_fwd"],
            max_fwd=stats["max_fwd"],
        ),
        events=page,
        fetched_at=int(time.time() * 1000),
    )


@router.get(
    "/status",
    response_model=PlatformStatusResponse,
    summary="플랫폼별 수신 상태·입출금 실패율",
    description=(
        "플랫폼(거래소)당 한 행 — 마지막 수신 시각, 상장 마켓 수(현물/선물), "
        "입출금 실패 횟수와 전체 업데이트 횟수.\n\n"
        "`POST /refresh` 가 market_snapshots 를 업데이트할 때마다 함께 갱신된다: "
        "전체 업데이트 횟수 +1, 그 회차에 입금 또는 출금 불가 코인이 하나라도 "
        "있었으면 실패 횟수 +1. `dw_fail_rate` = 실패 횟수 ÷ 전체 횟수."
    ),
)
async def platform_status(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PlatformStatusResponse:
    rows = await repository.get_platform_statuses(session)
    if not rows:
        raise MarketDataNotFoundError(
            "플랫폼 상태가 아직 없습니다. POST /refresh 가 한 번 돌면 생성됩니다.",
        )
    return PlatformStatusResponse(
        platforms=[
            PlatformStatusEntry(
                exchange=r.exchange,
                last_received_ts=r.last_received_ts,
                spot_market_count=r.spot_market_count,
                futures_market_count=r.futures_market_count,
                dw_fail_count=r.dw_fail_count,
                update_count=r.update_count,
                dw_fail_rate=(
                    r.dw_fail_count / r.update_count if r.update_count else 0.0
                ),
            )
            for r in rows
        ],
        fetched_at=int(time.time() * 1000),
    )
