"""가격 변동 이력 라우터 — 김프/역프 통계의 재료 조회.

세 엔드포인트다.

    GET  /history/coin  — 코인 하나의 변동 로그 (주/월 단위)
    GET  /history/fx    — 환율(USD/KRW 매매기준율)의 변동 로그 (주/월 단위)
    POST /history/sync  — 증분 수집 + 완결된 날 팩킹 (cron 이 주기 호출)

응답 형식의 핵심: **DB 는 절대 시각으로 저장하고, 응답은 상대 시각으로 준다.**
각 이벤트는 "직전 변동에서 몇 초 뒤에(dt), 얼마로(price), 얼마만큼(diff)
변했는가" 다. 절대 시각이 필요하면 first_ts 부터 dt 를 누적하면 된다 —
이 변환은 무손실이라 클라이언트가 절대 타임라인을 완전히 복원할 수 있다.

테스트 예시 (배포 서버 기준 — 로컬이면 http://localhost:8000 으로 바꾼다):
    코인 변동 로그 (이번 주, 최근 20건):
      http://3.34.104.16:8000/history/coin?exchange=binance&base=BTC&unit=week&limit=20
    특정 월 (8월 전체):
      http://3.34.104.16:8000/history/coin?exchange=upbit&base=BTC&unit=month&date=2026-08-12&limit=20
    환율 변동 로그:
      http://3.34.104.16:8000/history/fx?unit=week&limit=20
    증분 수집 (cron 이 부르는 것과 동일):
      curl -X POST -H "X-Refresh-Token: <토큰>" http://3.34.104.16:8000/history/sync
"""

from __future__ import annotations

import asyncio
import time
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.refresh import _check_refresh_token
from app.core.config import settings
from app.core.errors import InvalidRequestError, MarketDataNotFoundError
from app.db.database import get_session
from app.history import service as history_service

router = APIRouter(prefix="/history", tags=["history"])

#: 한 응답에 담는 이벤트 수 상한. 한 달치 변동은 수십만 건이 될 수 있어
#: offset/limit 페이지네이션을 강제한다 (총 개수는 count 로 알 수 있다).
MAX_EVENTS = 50_000
DEFAULT_EVENTS = 10_000


class ChangeEvent(BaseModel):
    """변동 한 건 — 직전 변동에서 몇 초 뒤에, 얼마로 변했는가."""

    dt: int = Field(
        ...,
        description=(
            "직전 이벤트로부터의 경과 초. 첫 이벤트는 0. "
            "(절대 시각 = first_ts + 자기까지의 dt 누적)"
        ),
    )
    price: float = Field(..., description="변동 후 가격")
    diff: float = Field(..., description="직전 가격 대비 변화량. 첫 이벤트는 0.")


class HistorySummary(BaseModel):
    """조회 구간의 변동 요약."""

    first_price: float = Field(..., description="구간 첫 이벤트 가격")
    last_price: float = Field(..., description="구간 마지막 이벤트 가격")
    min_price: float = Field(..., description="구간 최저가")
    max_price: float = Field(..., description="구간 최고가")
    change_percent: float = Field(
        ..., description="구간 전체 변동률 (last/first - 1) × 100"
    )


class HistoryResponse(BaseModel):
    """변동 로그 응답 — 코인·환율 공용 골격."""

    unit: Literal["week", "month"] = Field(..., description="조회 구간 단위")
    start: str = Field(..., description="구간 시작 (UTC, ISO8601)")
    end: str = Field(..., description="구간 끝 (UTC, ISO8601, exclusive)")

    first_ts: int | None = Field(
        ...,
        description=(
            "구간 내 첫 이벤트의 절대 시각 (epoch 초). 이벤트가 없으면 null. "
            "이 값에 dt 를 누적하면 절대 타임라인이 복원된다."
        ),
    )
    count: int = Field(..., description="구간 전체 이벤트 수")
    offset: int = Field(..., description="이번 페이지 시작 인덱스")
    returned: int = Field(..., description="이번 페이지에 담긴 이벤트 수")
    has_more: bool = Field(..., description="다음 페이지 존재 여부")

    summary: HistorySummary | None = Field(
        ..., description="구간 요약. 이벤트가 없으면 null."
    )
    events: list[ChangeEvent] = Field(..., description="변동 로그 (시각 순)")

    fetched_at: int = Field(..., description="응답 생성 시각 (epoch ms)")


class CoinHistoryResponse(HistoryResponse):
    """코인 변동 로그 응답."""

    exchange: str = Field(..., description="거래소 ID (upbit / binance)")
    base: str = Field(..., description="코인 심볼")
    quote: str = Field(..., description="가격 통화 (업비트=KRW, 바이낸스=USDT)")


class FxHistoryResponse(HistoryResponse):
    """환율 변동 로그 응답 — 하나은행 고시 USD/KRW 매매기준율."""


class SyncResult(BaseModel):
    """POST /history/sync 응답 — 무엇이 얼마나 새로 쌓였는지."""

    new_events: dict[str, int] = Field(
        ..., description="시리즈별 새 변동 이벤트 수 (예: 'upbit:BTC': 12)"
    )
    packed_days: int = Field(..., description="이번에 압축 청크로 팩킹한 날짜 수")
    failures: list[str] = Field(default_factory=list, description="실패한 시리즈")
    elapsed_ms: float = Field(..., description="처리 시간 (ms)")


def _build_log(
    events: list[tuple[int, Decimal]], offset: int, limit: int
) -> tuple[int | None, HistorySummary | None, list[ChangeEvent], bool]:
    """(절대 시각, 가격) 열을 상대 시각 로그 페이지로 변환한다.

    dt/diff 는 항상 **전체 열 기준의 직전 이벤트** 대비다 — 페이지를 나눠
    받아도 이어 붙이면 완전한 로그가 된다.
    """
    if not events:
        return None, None, [], False

    prices = [p for _, p in events]
    summary = HistorySummary(
        first_price=float(prices[0]),
        last_price=float(prices[-1]),
        min_price=float(min(prices)),
        max_price=float(max(prices)),
        change_percent=float((prices[-1] / prices[0] - 1) * 100),
    )

    page = events[offset : offset + limit]
    out: list[ChangeEvent] = []
    for i, (ts, price) in enumerate(page, start=offset):
        if i == 0:
            # 전체 열의 첫 이벤트 — 기준점이므로 dt/diff 가 0 이다.
            out.append(ChangeEvent(dt=0, price=float(price), diff=0.0))
        else:
            prev_ts, prev_price = events[i - 1]
            out.append(
                ChangeEvent(
                    dt=ts - prev_ts,
                    price=float(price),
                    diff=float(price - prev_price),
                )
            )
    return events[0][0], summary, out, offset + limit < len(events)


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


UnitParam = Annotated[
    Literal["week", "month"],
    Query(description="조회 구간 단위 — week(ISO 주) 또는 month(달력 월)"),
]
DateParam = Annotated[
    str | None,
    Query(
        alias="date",  # 파이썬 예약 이름을 피하되 쿼리 파라미터는 ?date= 로 받는다
        description=(
            "구간을 고를 기준 날짜 (YYYY-MM-DD). 그 날짜가 **속한** 주/월이 "
            "조회된다. 생략하면 오늘(UTC)."
        ),
        examples=["2026-08-13"],
    ),
]
OffsetParam = Annotated[int, Query(ge=0, description="이벤트 페이지 시작 인덱스")]
LimitParam = Annotated[
    int,
    Query(
        ge=1,
        le=MAX_EVENTS,
        description=f"페이지당 이벤트 수 (최대 {MAX_EVENTS:,})",
    ),
]


@router.get(
    "/coin",
    response_model=CoinHistoryResponse,
    summary="코인 가격 변동 로그 (주/월 단위)",
    description=(
        "한 코인의 가격이 **언제, 얼마 만에, 얼마로** 변했는지의 로그를 "
        "주/월 단위 구간으로 반환한다.\n\n"
        "데이터는 초 단위 변동 이력이다 — 업비트는 체결이 있던 초의 마지막 "
        "체결가, 바이낸스는 1초봉 종가 기준으로, **가격이 직전과 달라진 "
        "순간만** 저장돼 있다.\n\n"
        "DB 에는 절대 시각(epoch 초)으로 저장돼 있고, 응답의 각 이벤트는 "
        "직전 이벤트와의 **시간 차(dt 초)** 로 표현된다. `first_ts` 에 dt 를 "
        "누적하면 절대 타임라인이 그대로 복원된다.\n\n"
        "구간 전체 이벤트가 수십만 건일 수 있어 `offset`/`limit` 로 나눠 "
        "받는다. `count` 가 전체 개수, `has_more` 가 다음 페이지 유무다.\n\n"
        "가격 통화는 거래소 그대로다 — 업비트 KRW, 바이낸스 USDT. "
        "환율까지 얹은 김프 계산은 이 로그와 `/history/fx` 를 조합해서 한다."
    ),
)
async def coin_history(
    session: Annotated[AsyncSession, Depends(get_session)],
    exchange: Annotated[
        Literal["upbit", "binance"],
        Query(description="거래소 ID"),
    ],
    base: Annotated[str, Query(description="코인 심볼", examples=["BTC"])],
    unit: UnitParam,
    date_: DateParam = None,
    offset: OffsetParam = 0,
    limit: LimitParam = DEFAULT_EVENTS,
) -> CoinHistoryResponse:
    start_ts, end_ts = history_service.period_range(unit, _parse_anchor(date_))
    events = await history_service.load_price_events(
        session, exchange, base.upper(), start_ts, end_ts
    )
    if not events:
        raise MarketDataNotFoundError(
            f"{exchange} {base.upper()} 의 해당 구간 변동 이력이 DB 에 없습니다. "
            "backfill 스크립트나 POST /history/sync 로 수집했는지 확인하세요.",
            detail={"exchange": exchange, "base": base.upper(), "unit": unit},
        )

    first_ts, summary, page, has_more = _build_log(events, offset, limit)
    return CoinHistoryResponse(
        exchange=exchange,
        base=base.upper(),
        quote="KRW" if exchange == "upbit" else settings.fx_stablecoin,
        unit=unit,
        start=datetime.fromtimestamp(start_ts, tz=timezone.utc).isoformat(),
        end=datetime.fromtimestamp(end_ts, tz=timezone.utc).isoformat(),
        first_ts=first_ts,
        count=len(events),
        offset=offset,
        returned=len(page),
        has_more=has_more,
        summary=summary,
        events=page,
        fetched_at=int(time.time() * 1000),
    )


@router.get(
    "/fx",
    response_model=FxHistoryResponse,
    summary="환율(USD/KRW) 변동 로그 (주/월 단위)",
    description=(
        "**하나은행 고시 USD/KRW 매매기준율**의 변동 로그를 주/월 단위 "
        "구간으로 반환한다. 코인 로그(`/history/coin`)와 같은 형식이지만 "
        "환율은 거래소·코인 개념이 없어 별도 엔드포인트다.\n\n"
        "은행은 하루 1,300~2,000회(평균 약 44초 간격) 고시하며, 그중 "
        "매매기준율이 **직전과 달라진 고시만** 저장돼 있다. dt 의 의미는 "
        "코인 로그와 같다 — 직전 변동에서 몇 초 뒤에 변했는가.\n\n"
        "주말·야간에는 외환시장이 멈추므로 dt 가 수만 초로 커지는 것이 "
        "정상이다 (금요일 마지막 고시가 월요일 아침까지 유지된다)."
    ),
)
async def fx_history(
    session: Annotated[AsyncSession, Depends(get_session)],
    unit: UnitParam,
    date_: DateParam = None,
    offset: OffsetParam = 0,
    limit: LimitParam = DEFAULT_EVENTS,
) -> FxHistoryResponse:
    start_ts, end_ts = history_service.period_range(unit, _parse_anchor(date_))
    events = await history_service.load_fx_events(session, start_ts, end_ts)
    if not events:
        raise MarketDataNotFoundError(
            "해당 구간의 환율 변동 이력이 DB 에 없습니다. "
            "backfill 스크립트나 POST /history/sync 로 수집했는지 확인하세요.",
            detail={"unit": unit},
        )

    first_ts, summary, page, has_more = _build_log(events, offset, limit)
    return FxHistoryResponse(
        unit=unit,
        start=datetime.fromtimestamp(start_ts, tz=timezone.utc).isoformat(),
        end=datetime.fromtimestamp(end_ts, tz=timezone.utc).isoformat(),
        first_ts=first_ts,
        count=len(events),
        offset=offset,
        returned=len(page),
        has_more=has_more,
        summary=summary,
        events=page,
        fetched_at=int(time.time() * 1000),
    )


#: 동시 sync 를 막는다 — 커서 기반 증분 수집은 순차 실행을 전제로 한다.
#: (밀린 데이터를 따라잡는 sync 가 1분을 넘기면, cron 이 띄운 다음 호출이
#:  같은 커서를 읽어 같은 구간을 중복으로 받아 레이트리밋만 태우게 된다)
_sync_lock = asyncio.Lock()


@router.post(
    "/sync",
    response_model=SyncResult,
    summary="변동 이력 증분 수집 + 팩킹 (cron 용)",
    description=(
        "설정된 코인들(`HISTORY_BASES`)의 새 가격 변동과 최신 환율 고시를 "
        "수집해 스테이징에 쌓고, UTC 자정이 지나 완결된 날은 압축 청크로 "
        "팩킹한다.\n\n"
        "각 시리즈는 커서(마지막 반영 시각)를 기억하고, 시리즈 하나가 끝날 "
        "때마다 커밋한다 — 일부 시리즈가 실패해도 성공한 것은 저장된다 "
        "(`failures` 에 표시). 이미 실행 중인 sync 가 있으면 이번 호출은 "
        "건너뛴다 (동시 실행 방지). 1분 주기 cron 을 권장한다.\n\n"
        "서버에 `REFRESH_TOKEN` 이 설정돼 있으면 `X-Refresh-Token` 헤더가 "
        "필요하다."
    ),
)
async def sync_history(
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[None, Depends(_check_refresh_token)],
) -> SyncResult:
    started = time.perf_counter()

    if _sync_lock.locked():
        # 직전 sync 가 아직 도는 중 — 대기하지 않고 이번 틱은 넘어간다.
        # (커서 기반이라 다음 성공한 sync 가 밀린 구간을 어차피 따라잡는다)
        return SyncResult(
            new_events={},
            packed_days=0,
            failures=["이미 실행 중인 sync 가 있어 이번 호출은 건너뜁니다."],
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
        )

    async with _sync_lock:
        now_ts = int(time.time())
        new_events: dict[str, int] = {}
        failures: list[str] = []

        async def run_step(label: str, coro) -> int | None:
            """한 단계를 실행하고 성공하면 커밋, 실패하면 롤백한다.

            같은 세션을 이어 쓰므로, 실패한 트랜잭션을 롤백하지 않으면
            이후 모든 단계가 PendingRollbackError 로 연쇄 실패한다.
            단계별 커밋 덕분에 한 시리즈의 실패가 다른 시리즈의 성과를
            날리지도 않는다.
            """
            try:
                result = await coro
                await session.commit()
                return result
            except Exception as exc:  # noqa: BLE001 — 한 단계 실패가 전체를 막으면 안 된다
                await session.rollback()
                failures.append(f"{label} — {type(exc).__name__}: {exc}")
                return None

        for base in settings.history_bases:
            base = base.upper()
            saved = await run_step(
                f"upbit:{base}",
                history_service.sync_upbit(session, base, now_ts=now_ts),
            )
            if saved is not None:
                new_events[f"upbit:{base}"] = saved
            saved = await run_step(
                f"binance:{base}",
                history_service.sync_binance(session, base, now_ts=now_ts),
            )
            if saved is not None:
                new_events[f"binance:{base}"] = saved

        saved = await run_step("fx:USD", history_service.sync_fx(session))
        if saved is not None:
            new_events["fx:USD"] = saved

        # 완결된 날 팩킹 — sync 가 하루에 한 번은 돌기만 하면 자동으로 처리된다.
        packed = 0
        for base in settings.history_bases:
            base = base.upper()
            for exchange in ("upbit", "binance"):
                result = await run_step(
                    f"pack:{exchange}:{base}",
                    history_service.pack_price_days(
                        session, exchange, base, now_ts=now_ts
                    ),
                )
                packed += result or 0
        result = await run_step(
            "pack:fx", history_service.pack_fx_days(session, now_ts=now_ts)
        )
        packed += result or 0

        return SyncResult(
            new_events=new_events,
            packed_days=packed,
            failures=failures,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
        )
