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
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import InvalidRequestError, MarketDataNotFoundError
from app.db import repository
from app.db.database import get_session
from app.db.models import PremiumArchive
from app.history import service as history_service
from app.history.streaks import (
    DEFAULT_MAX_GAP_SECONDS,
    StreakStats,
    find_segments,
)
from app.models.streak import (
    BulkStreakResponse,
    CoinStreaks,
    OverallStats,
    StreakDirection,
    StreakResponse,
    StreakSegment,
)

#: 응답의 사람이 읽는 시각은 KST 로 준다 (서비스 사용자가 국내 기준이다).
KST = timezone(timedelta(hours=9))

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


class DwFailWindow(BaseModel):
    """입출금 실패가 이어진 구간 하나 (수집 상태 창의 결측 구간 표시용)."""

    start_ts: int = Field(..., description="구간 시작 (epoch 초)")
    end_ts: int = Field(..., description="구간 끝 (epoch 초, 마지막 관측 시각)")
    start: str = Field(..., description="구간 시작 (KST ISO 8601)")
    end: str = Field(..., description="구간 끝 (KST ISO 8601)")
    duration_seconds: int = Field(
        ..., description="구간 길이 초 (end_ts - start_ts, 관측 1회짜리는 0)"
    )
    count: int = Field(..., description="구간에 든 실패 관측 횟수")


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
    dw_fail_windows: list[DwFailWindow] = Field(
        ...,
        description=(
            "최근 보존 기간(retention_seconds) 안의 입출금 실패 구간 목록 "
            "(시각 순). 실패 관측 시각이 max_gap 초 이내로 이어지면 한 구간이다."
        ),
    )


class PlatformStatusResponse(BaseModel):
    """플랫폼별 수신 상태 목록."""

    platforms: list[PlatformStatusEntry] = Field(..., description="플랫폼별 상태")
    retention_seconds: int = Field(
        ..., description="실패 구간이 보관·표시되는 기간 초 (기본 24시간)"
    )
    max_gap_seconds: int = Field(
        ..., description="실패 관측을 한 구간으로 잇는 최대 간격 초"
    )
    fetched_at: int = Field(..., description="응답 생성 시각 (epoch ms)")


def merge_fail_windows(
    ts_list: list[int], max_gap: int, retention_start: int
) -> list["DwFailWindow"]:
    """실패 관측 시각들을 구간으로 잇는다.

    이웃한 시각이 ``max_gap`` 초 이내면 같은 구간, 넘게 벌어지면 새 구간이다
    (streaks 의 구간 끊기와 같은 규칙). ``retention_start`` 이전 시각은
    이미 지워졌어야 하지만, 혹시 남아 있어도 표시 창 밖이므로 걸러 낸다.
    """
    out: list[DwFailWindow] = []
    start = prev = None
    count = 0

    def _close() -> None:
        out.append(
            DwFailWindow(
                start_ts=start,
                end_ts=prev,
                start=_kst_iso(start),
                end=_kst_iso(prev),
                duration_seconds=prev - start,
                count=count,
            )
        )

    for ts in ts_list:
        if ts < retention_start:
            continue
        if start is None:
            start, prev, count = ts, ts, 1
        elif ts - prev <= max_gap:
            prev, count = ts, count + 1
        else:
            _close()
            start, prev, count = ts, ts, 1
    if start is not None:
        _close()
    return out


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
        "있었으면 실패 횟수 +1. `dw_fail_rate` = 실패 횟수 ÷ 전체 횟수.\n\n"
        "`dw_fail_windows` 는 최근 24시간(`retention_seconds`) 안에서 실패가 "
        "**언제부터 언제까지** 이어졌는지의 구간 목록이다 — 실패 횟수가 +1 될 때 "
        "같이 기록된 시각들을, `max_gap` 초 이내로 이어지는 것끼리 묶은 것이다. "
        "보존 기간이 지난 기록은 refresh 가 돌 때마다 DB 에서 지워진다."
    ),
)
async def platform_status(
    session: Annotated[AsyncSession, Depends(get_session)],
    max_gap: Annotated[
        int,
        Query(
            ge=1,
            description=(
                "실패 관측 시각이 이 초 이내로 이어지면 한 구간으로 묶는다 "
                f"(기본 {DEFAULT_MAX_GAP_SECONDS}초 — 정상 수집 간격은 약 60초)"
            ),
        ),
    ] = DEFAULT_MAX_GAP_SECONDS,
) -> PlatformStatusResponse:
    rows = await repository.get_platform_statuses(session)
    if not rows:
        raise MarketDataNotFoundError(
            "플랫폼 상태가 아직 없습니다. POST /refresh 가 한 번 돌면 생성됩니다.",
        )
    retention = settings.dw_fail_retention_seconds
    since_ts = int(time.time()) - retention
    fail_events = await repository.get_dw_fail_events(session, since_ts)
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
                dw_fail_windows=merge_fail_windows(
                    fail_events.get(r.exchange, []), max_gap, since_ts
                ),
            )
            for r in rows
        ],
        retention_seconds=retention,
        max_gap_seconds=max_gap,
        fetched_at=int(time.time() * 1000),
    )


def _kst_iso(ts: int) -> str:
    """epoch 초 → KST ISO 8601 문자열."""
    return datetime.fromtimestamp(ts, tz=KST).isoformat()


def _coin_streaks(
    base: str,
    fwd_points: list[tuple[int, float]],
    rev_points: list[tuple[int, float]],
    threshold: float,
    max_gap: int,
) -> CoinStreaks:
    """한 코인의 (ts, 값) 목록으로 단건 응답과 같은 구간·요약 블록을 만든다."""
    kimp_stats = find_segments(fwd_points, threshold, max_gap_seconds=max_gap)
    reverse_stats = find_segments(rev_points, threshold, max_gap_seconds=max_gap)
    all_durations = [
        s.duration_seconds for s in (*kimp_stats.segments, *reverse_stats.segments)
    ]
    n = len(fwd_points)
    return CoinStreaks(
        base=base,
        scanned=n,
        last_ts=fwd_points[-1][0],
        kimp=_to_direction(kimp_stats),
        reverse=_to_direction(reverse_stats),
        overall=OverallStats(
            max_kimp_percent=max((v for _, v in fwd_points), default=0.0),
            avg_kimp_percent=(sum(v for _, v in fwd_points) / n) if n else 0.0,
            max_reverse_percent=max((v for _, v in rev_points), default=0.0),
            avg_reverse_percent=(sum(v for _, v in rev_points) / n) if n else 0.0,
            max_duration_seconds=max(all_durations, default=0),
            avg_duration_seconds=(
                sum(all_durations) / len(all_durations) if all_durations else 0.0
            ),
            segment_count=len(all_durations),
        ),
    )


def _to_direction(stats: StreakStats) -> StreakDirection:
    return StreakDirection(
        count=stats.count,
        max_duration_seconds=stats.max_duration_seconds,
        avg_duration_seconds=stats.avg_duration_seconds,
        max_percent=stats.max_percent,
        avg_percent=stats.avg_percent,
        segments=[
            StreakSegment(
                start_ts=s.start_ts,
                end_ts=s.end_ts,
                start=_kst_iso(s.start_ts),
                end=_kst_iso(s.end_ts),
                duration_seconds=s.duration_seconds,
                samples=s.samples,
                max_percent=s.max_percent,
                avg_percent=s.avg_percent,
            )
            for s in stats.segments
        ],
    )


@router.get(
    "/streaks",
    response_model=StreakResponse,
    summary="김프/역프 구간 통계 (기준치 이상이 언제부터 언제까지)",
    description=(
        "한 코인이 **언제까지 김프였고 언제까지 역프였는지**를 구간으로 묶어 "
        "돌려준다 — 기록/통계 창의 요약 데이터.\n\n"
        "`threshold` 로 기준치를 주면 그 값 **이상**인 기록만 남기고, 시각이 "
        "이어지는 것끼리 묶어 한 구간으로 본다. 기준치를 올릴수록 구간이 잘게 "
        "쪼개진다.\n\n"
        "예 — 값이 `0 1 3 6 29 4 31` 이고 `threshold=4` 면 `6 29 4 31` 이 남아 "
        "**구간 1개**다. `threshold=5` 면 4 가 탈락해 이어짐이 끊기고 "
        "`(6 29)`, `(31)` **구간 2개**가 된다.\n\n"
        "김프(`fwd`)와 역프(`rev`)는 **각각 따로** 센다. 둘은 부호만 뒤집은 같은 "
        "값이 아니다 — 양쪽 호가 차이 때문에 둘 다 음수인 순간이 많아서, "
        "절댓값이 아니라 방향별로 `>= threshold` 를 본다.\n\n"
        "수집이 멈췄던 구멍은 이어 붙이지 않는다. 이웃한 기록이 "
        "`max_gap` 초를 넘겨 벌어져 있으면 거기서 구간을 끊는다 — 그러지 않으면 "
        "'몇 시간 연속 김프' 라는 없던 사실이 만들어진다."
    ),
)
async def premium_streaks(
    session: Annotated[AsyncSession, Depends(get_session)],
    base: Annotated[str, Query(description="코인 심볼", examples=["BTC"])],
    threshold: Annotated[
        float,
        Query(
            ge=0,
            description="기준치 %. 이 값 **이상**인 기록만 구간에 든다 (초과가 아님)",
            examples=[1.0],
        ),
    ] = 0.0,
    dom: Annotated[
        Literal["upbit", "bithumb"], Query(description="국내 거래소 ID")
    ] = "upbit",
    fx: Annotated[Literal["binance"], Query(description="해외 거래소 ID")] = "binance",
    start: Annotated[
        int | None,
        Query(description="조회 시작 (epoch 초). 생략하면 기록의 처음부터"),
    ] = None,
    end: Annotated[
        int | None,
        Query(description="조회 종료 (epoch 초, 미포함). 생략하면 지금까지"),
    ] = None,
    max_gap: Annotated[
        int,
        Query(
            ge=1,
            description=(
                "이 초를 넘겨 기록이 벌어지면 구간을 끊는다 "
                f"(기본 {DEFAULT_MAX_GAP_SECONDS}초 — 정상 수집 간격은 약 60초)"
            ),
        ),
    ] = DEFAULT_MAX_GAP_SECONDS,
) -> StreakResponse:
    symbol = base.upper()
    bounds = await repository.get_premium_bounds(session, dom, fx, symbol)
    if bounds is None:
        raise MarketDataNotFoundError(
            f"{dom}×{fx} {symbol} 의 김프 기록이 없습니다. refresh 가 돌고 "
            "있는지, 과거 구간은 bulk_archive 스크립트를 실행했는지 확인하세요.",
            detail={"dom": dom, "fx": fx, "base": symbol},
        )
    first_ts, last_ts = bounds

    start_ts = first_ts if start is None else start
    end_ts = (int(time.time()) + 1) if end is None else end
    if end_ts <= start_ts:
        raise InvalidRequestError(
            "end 는 start 보다 뒤여야 합니다.",
            detail={"start": start_ts, "end": end_ts},
        )

    rows = await repository.get_premium_range(
        session, dom, fx, symbol, start_ts, end_ts
    )

    kimp_stats = find_segments(
        [(r.ts, r.fwd) for r in rows], threshold, max_gap_seconds=max_gap
    )
    reverse_stats = find_segments(
        [(r.ts, r.rev) for r in rows], threshold, max_gap_seconds=max_gap
    )

    # 전체 요약 — 기준치를 적용하기 **전** 기록 그대로. 지속 시간만은 구간
    # 개념이라 기준치를 타므로, 두 방향의 구간을 합쳐서 낸다.
    all_durations = [
        s.duration_seconds for s in (*kimp_stats.segments, *reverse_stats.segments)
    ]
    overall = OverallStats(
        max_kimp_percent=max((r.fwd for r in rows), default=0.0),
        avg_kimp_percent=(sum(r.fwd for r in rows) / len(rows)) if rows else 0.0,
        max_reverse_percent=max((r.rev for r in rows), default=0.0),
        avg_reverse_percent=(sum(r.rev for r in rows) / len(rows)) if rows else 0.0,
        max_duration_seconds=max(all_durations, default=0),
        avg_duration_seconds=(
            sum(all_durations) / len(all_durations) if all_durations else 0.0
        ),
        segment_count=len(all_durations),
    )

    return StreakResponse(
        base=symbol,
        dom=dom,
        fx=fx,
        threshold_percent=threshold,
        max_gap_seconds=max_gap,
        start_ts=start_ts,
        end_ts=end_ts,
        scanned=len(rows),
        kimp=_to_direction(kimp_stats),
        reverse=_to_direction(reverse_stats),
        overall=overall,
        last_updated_ts=last_ts,
        last_updated=_kst_iso(last_ts),
        fetched_at=int(time.time() * 1000),
    )


@router.get(
    "/streaks/bulk",
    response_model=BulkStreakResponse,
    summary="전 코인 김프/역프 구간 통계 (한 국내 거래소, 요청 1회)",
    description=(
        "한 국내 거래소의 **기록이 있는 모든 코인**(또는 `bases` 로 지정한 "
        "코인들)의 김프/역프 구간 통계를 한 번에 돌려준다 — 기록/통계 창이 "
        "코인마다 `/history/streaks` 를 부르던 것을 요청 1회로 줄인다.\n\n"
        "구간 규칙은 단건과 같다: `threshold` **이상**인 기록이 시각으로 "
        "이어지면 한 구간, 이웃 기록이 `max_gap` 초를 넘겨 벌어지면 끊는다.\n\n"
        "단건과 다른 점은 `bucket` 리샘플링이다 (기본 60초). 버킷마다 마지막 "
        "기록 하나만 남긴다 — 대량 백필 데이터(초 단위)와 실시간 기록(약 "
        "60초 단위)의 밀도를 맞춰야 코인끼리 구간 '횟수'를 비교할 수 있고, "
        "훑는 행 수도 수십 배 줄어든다. 원본 그대로 보려면 `bucket=0`.\n\n"
        "구간 내 기록이 하나도 없으면 404 가 아니라 **빈 coins 목록**이다 — "
        "벌크 조회에서 '아무 코인도 없음'은 정상 상태다."
    ),
)
async def premium_streaks_bulk(
    session: Annotated[AsyncSession, Depends(get_session)],
    threshold: Annotated[
        float,
        Query(
            ge=0,
            description="기준치 %. 이 값 **이상**인 기록만 구간에 든다 (초과가 아님)",
            examples=[0.5],
        ),
    ] = 0.0,
    dom: Annotated[
        Literal["upbit", "bithumb"], Query(description="국내 거래소 ID")
    ] = "upbit",
    fx: Annotated[Literal["binance"], Query(description="해외 거래소 ID")] = "binance",
    start: Annotated[
        int | None,
        Query(description="조회 시작 (epoch 초). 생략하면 기록의 처음부터"),
    ] = None,
    end: Annotated[
        int | None,
        Query(description="조회 종료 (epoch 초, 미포함). 생략하면 지금까지"),
    ] = None,
    max_gap: Annotated[
        int,
        Query(
            ge=1,
            description=(
                "이 초를 넘겨 기록이 벌어지면 구간을 끊는다 "
                f"(기본 {DEFAULT_MAX_GAP_SECONDS}초 — 정상 수집 간격은 약 60초)"
            ),
        ),
    ] = DEFAULT_MAX_GAP_SECONDS,
    bucket: Annotated[
        int,
        Query(
            ge=0,
            description=(
                "리샘플링 버킷 (초). 버킷마다 마지막 기록 하나만 남긴다. "
                "0 이면 리샘플링하지 않는다 (기본 60 — 실시간 수집 간격)"
            ),
        ),
    ] = 60,
    bases: Annotated[
        str | None,
        Query(
            description="코인 심볼 목록 (쉼표 구분). 생략하면 기록이 있는 전 코인",
            examples=["BTC,ETH,XRP"],
        ),
    ] = None,
) -> BulkStreakResponse:
    start_ts = 0 if start is None else start
    end_ts = (int(time.time()) + 1) if end is None else end
    if end_ts <= start_ts:
        raise InvalidRequestError(
            "end 는 start 보다 뒤여야 합니다.",
            detail={"start": start_ts, "end": end_ts},
        )
    base_list = (
        [b.strip().upper() for b in bases.split(",") if b.strip()] if bases else None
    )

    result = await repository.stream_premium_points(
        session,
        dom,
        fx,
        start_ts,
        end_ts,
        bases=base_list,
    )

    # 코인 하나 분량만 메모리에 들고, 코인이 바뀔 때마다 구간을 계산해 비운다.
    # bucket 리샘플링도 여기서 한다 — 스트림이 시각 오름차순이므로 같은 버킷의
    # 기록이 또 오면 마지막 것으로 덮어쓰면 된다 (SQL 로 하는 것보다 빠르다,
    # repository.stream_premium_points 참고).
    coins: list[CoinStreaks] = []
    current: str | None = None
    current_bucket: int | None = None
    fwd_points: list[tuple[int, float]] = []
    rev_points: list[tuple[int, float]] = []

    async for row_base, ts, fwd, rev in result:
        if row_base != current:
            if current is not None:
                coins.append(
                    _coin_streaks(current, fwd_points, rev_points, threshold, max_gap)
                )
            current = row_base
            current_bucket = None
            fwd_points, rev_points = [], []
        bucket_id = ts // bucket if bucket > 0 else ts
        if bucket_id == current_bucket:
            fwd_points[-1] = (ts, fwd)
            rev_points[-1] = (ts, rev)
        else:
            fwd_points.append((ts, fwd))
            rev_points.append((ts, rev))
            current_bucket = bucket_id
    if current is not None:
        coins.append(
            _coin_streaks(current, fwd_points, rev_points, threshold, max_gap)
        )

    return BulkStreakResponse(
        dom=dom,
        fx=fx,
        threshold_percent=threshold,
        max_gap_seconds=max_gap,
        bucket_seconds=bucket,
        start_ts=start_ts,
        end_ts=end_ts,
        coin_count=len(coins),
        coins=coins,
        fetched_at=int(time.time() * 1000),
    )
