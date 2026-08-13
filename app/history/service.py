"""이력 서비스 — 변동 축약, 팩킹, 주/월 구간 계산, 증분 수집(sync).

데이터 흐름

    수집(백필 스크립트 / POST /history/sync)
        거래소 API → (ts, Decimal 가격) → keep_changes() 로 변동만 남김
        → 스테이징 테이블(절대 epoch 초 + 십진 문자열)

    팩킹 (sync 때마다 자동)
        완결된 UTC 하루의 스테이징 행 → 스케일 정수 → codec 압축 청크
        → 스테이징 삭제

    조회 (GET /history/coin, /history/fx)
        구간에 걸친 청크 디코딩 + 아직 팩킹 전인 스테이징 → 병합
        → 라우터가 상대 시간(직전 변동에서 몇 초 뒤) 로그로 변환
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.history import binance as binance_history
from app.history import codec, hana, store
from app.history import upbit as upbit_history
from app.history.store import ChunkData

#: 이력을 지원하는 거래소. 빗썸은 초봉 API 미확인이라 아직 제외한다.
HISTORY_EXCHANGES = ("upbit", "binance")


# ----------------------------------------------------------------------
# 변동 축약 — "가격이 달라진 순간"만 남긴다
# ----------------------------------------------------------------------


def keep_changes(
    points: list[tuple[int, Decimal]], seed: Decimal | None
) -> list[tuple[int, Decimal]]:
    """직전 가격과 같은 포인트를 버린다.

    ``seed`` 는 구간 직전의 마지막 가격 — 이것과 같은 첫 포인트들도 버린다.
    (seed 가 None 이면 비교 대상이 없으므로 첫 포인트는 무조건 남는다)

    같은 가격의 연속 구간은 가격 경로에 정보가 없으므로 이 축약은
    "가격이 언제 얼마로 변했는가" 관점에서 무손실이다.
    """
    out: list[tuple[int, Decimal]] = []
    prev = seed
    for ts, price in points:
        if prev is None or price != prev:
            out.append((ts, price))
            prev = price
    return out


# ----------------------------------------------------------------------
# 청크 만들기 / 풀기
# ----------------------------------------------------------------------


def build_chunk(day: date, points: list[tuple[int, Decimal]]) -> ChunkData:
    """하루치 변동 이벤트를 압축 청크 값 묶음으로 만든다.

    스케일(10^n 배율)은 그 날 데이터의 소수 자릿수에 맞춰 정해지고
    청크에 기록된다 — 코인·날짜마다 달라도 복원에는 지장이 없다.
    """
    prices = [p for _, p in points]
    scale = codec.decimal_scale(prices)
    scaled = [(ts, codec.to_scaled(p, scale)) for ts, p in points]
    # encode_points_verified — 인코딩 직후 디코딩해 원본과 대조한다 (무손실 보증).
    blob = codec.encode_points_verified(scaled)
    values = [v for _, v in scaled]
    return ChunkData(
        day=day,
        codec=codec.CODEC_VERSION,
        price_scale=scale,
        n_points=len(points),
        first_ts=scaled[0][0],
        last_ts=scaled[-1][0],
        first_price=values[0],
        last_price=values[-1],
        min_price=min(values),
        max_price=max(values),
        data=blob,
    )


def decode_chunk(
    *, data: bytes, price_scale: int, n_points: int
) -> list[tuple[int, Decimal]]:
    """청크 블롭을 (epoch 초, Decimal 가격) 열로 되돌린다.

    메타 컬럼(n_points)과 대조해 블롭 손상을 조기에 잡는다.
    """
    points = codec.decode_points(data)
    if len(points) != n_points:
        raise ValueError(
            f"청크 손상 — 메타는 {n_points}개인데 블롭에는 {len(points)}개"
        )
    return [(ts, codec.from_scaled(v, price_scale)) for ts, v in points]


# ----------------------------------------------------------------------
# 주 / 월 구간 계산
# ----------------------------------------------------------------------


def period_range(unit: str, anchor: date) -> tuple[int, int]:
    """unit(week|month)과 기준 날짜로 [시작, 끝) epoch 초 구간(UTC)을 만든다.

    - week  : anchor 가 속한 ISO 주 (월요일 00:00 ~ 다음 월요일 00:00)
    - month : anchor 가 속한 달력 월 (1일 00:00 ~ 다음 달 1일 00:00)
    """
    if unit == "week":
        start_day = anchor - timedelta(days=anchor.weekday())
        end_day = start_day + timedelta(days=7)
    elif unit == "month":
        start_day = anchor.replace(day=1)
        end_day = (start_day + timedelta(days=32)).replace(day=1)
    else:
        raise ValueError(f"unit 은 week 또는 month 여야 합니다: {unit}")

    to_ts = lambda d: int(  # noqa: E731 — 두 줄짜리 지역 변환용
        datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()
    )
    return to_ts(start_day), to_ts(end_day)


def _utc_day(ts: int) -> date:
    return datetime.fromtimestamp(ts, tz=timezone.utc).date()


def _day_start_ts(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())


# ----------------------------------------------------------------------
# 조회 — 청크 + 스테이징 병합
# ----------------------------------------------------------------------


async def load_price_events(
    session: AsyncSession,
    exchange: str,
    base: str,
    start_ts: int,
    end_ts: int,
) -> list[tuple[int, Decimal]]:
    """[start_ts, end_ts) 구간의 가격 변동 이벤트 (절대 시각, 오름차순).

    완결된 날은 청크에서, 아직 팩킹 전인 날은 스테이징에서 온다.
    같은 시각이 양쪽에 있으면 스테이징이 이긴다 (더 나중에 수집된 값).
    """
    merged: dict[int, Decimal] = {}

    chunks = await store.get_price_chunks(
        session, exchange, base, _utc_day(start_ts), _utc_day(end_ts - 1)
    )
    for chunk in chunks:
        for ts, price in decode_chunk(
            data=chunk.data,
            price_scale=chunk.price_scale,
            n_points=chunk.n_points,
        ):
            if start_ts <= ts < end_ts:
                merged[ts] = price

    for ts, price in await store.get_price_points(
        session, exchange, base, start_ts, end_ts
    ):
        merged[ts] = Decimal(price)

    # 청크·스테이징 경계에서 생길 수 있는 연속 같은-가격 이벤트를 정리한다.
    return keep_changes(sorted(merged.items()), None)


async def load_fx_events(
    session: AsyncSession, start_ts: int, end_ts: int
) -> list[tuple[int, Decimal]]:
    """[start_ts, end_ts) 구간의 환율 변동 이벤트 — 구조는 가격과 동일."""
    merged: dict[int, Decimal] = {}

    for chunk in await store.get_fx_chunks(
        session, _utc_day(start_ts), _utc_day(end_ts - 1)
    ):
        for ts, price in decode_chunk(
            data=chunk.data,
            price_scale=chunk.price_scale,
            n_points=chunk.n_points,
        ):
            if start_ts <= ts < end_ts:
                merged[ts] = price

    for ts, price in await store.get_fx_points(session, start_ts, end_ts):
        merged[ts] = Decimal(price)

    return keep_changes(sorted(merged.items()), None)


# ----------------------------------------------------------------------
# 팩킹 — 완결된 하루를 스테이징 → 압축 청크로
# ----------------------------------------------------------------------


async def pack_price_days(
    session: AsyncSession, exchange: str, base: str, *, now_ts: int
) -> int:
    """오늘(UTC) 이전의 스테이징 행을 날짜별 청크로 옮긴다.

    이미 청크가 있는 날짜는 병합한다 (백필과 주기 수집이 겹쳐도 안전).
    스테이징 삭제는 **이번에 실제로 팩킹한 이벤트들만** 지운다 — 그 사이
    다른 트랜잭션이 커밋한 새 행을 실수로 지우지 않기 위해서다.
    Returns: 팩킹한 날짜 수.
    """
    today_start = _day_start_ts(_utc_day(now_ts))
    staged = await store.get_price_points(session, exchange, base, 0, today_start)
    if not staged:
        return 0

    by_day: dict[date, list[tuple[int, Decimal]]] = {}
    for ts, price in staged:
        by_day.setdefault(_utc_day(ts), []).append((ts, Decimal(price)))

    for day, points in sorted(by_day.items()):
        existing = await store.get_price_chunks(session, exchange, base, day, day)
        if existing:
            # 같은 날 청크가 이미 있으면 (재팩킹·백필 겹침) 풀어서 합친다.
            merged = {
                ts: price
                for ts, price in decode_chunk(
                    data=existing[0].data,
                    price_scale=existing[0].price_scale,
                    n_points=existing[0].n_points,
                )
            }
            merged.update(dict(points))
            points = sorted(merged.items())
        # 병합 과정에서 생길 수 있는 "연속 같은 가격" 을 정리해
        # 변동-로그 불변식(인접 이벤트의 가격은 항상 다르다)을 지킨다.
        # (예: 커서 없이 시작한 sync 의 첫 관측과 백필이 겹치는 경우)
        points = keep_changes(points, None)
        await store.upsert_price_chunk(
            session, exchange, base, build_chunk(day, points)
        )

    await store.delete_price_points(
        session, exchange, base, [ts for ts, _ in staged]
    )
    return len(by_day)


async def pack_fx_days(session: AsyncSession, *, now_ts: int) -> int:
    """환율 스테이징의 완결된 날들을 청크로 옮긴다 — 가격과 같은 절차."""
    today_start = _day_start_ts(_utc_day(now_ts))
    staged = await store.get_fx_points(session, 0, today_start)
    if not staged:
        return 0

    by_day: dict[date, list[tuple[int, Decimal]]] = {}
    for ts, price in staged:
        by_day.setdefault(_utc_day(ts), []).append((ts, Decimal(price)))

    for day, points in sorted(by_day.items()):
        existing = await store.get_fx_chunks(session, day, day)
        if existing:
            merged = {
                ts: price
                for ts, price in decode_chunk(
                    data=existing[0].data,
                    price_scale=existing[0].price_scale,
                    n_points=existing[0].n_points,
                )
            }
            merged.update(dict(points))
            points = sorted(merged.items())
        points = keep_changes(points, None)  # 변동-로그 불변식 유지
        await store.upsert_fx_chunk(session, build_chunk(day, points))

    await store.delete_fx_points(session, [ts for ts, _ in staged])
    return len(by_day)


# ----------------------------------------------------------------------
# 증분 수집(sync) — 커서 이후의 새 데이터만 가져온다
# ----------------------------------------------------------------------


async def sync_upbit(session: AsyncSession, base: str, *, now_ts: int) -> int:
    """업비트 초봉을 커서 이후부터 받아 변동만 스테이징에 쌓는다.

    Returns: 새로 저장한 변동 이벤트 수.
    """
    cursor = await store.get_cursor(session, "upbit", base)
    if cursor is None:
        # 첫 수집 — 너무 먼 과거는 백필 스크립트 몫이다. 최근 구간만 시작한다.
        start_ts = now_ts - settings.history_sync_lookback_seconds
        seed: Decimal | None = None
    else:
        start_ts = cursor.last_ts + 1
        seed = Decimal(cursor.last_price)

    # end 를 now_ts(현재 진행 중인 초, exclusive)로 잡아 **아직 닫히지 않은
    # 캔들은 받지 않는다.** 열린 초를 저장하면 그 초의 최종 체결가가 아니라
    # 중간값이 영구히 박제된다 (커서가 지나가면 다시 받지 않으므로).
    observed = await upbit_history.fetch_seconds_range(base, start_ts, now_ts)
    if not observed:
        return 0

    changes = keep_changes(observed, seed)
    await store.add_price_points(
        session, "upbit", base, [(ts, str(p)) for ts, p in changes]
    )
    # 커서는 "마지막으로 관측한" 포인트까지 전진한다 — 변동이 없었어도
    # 같은 구간을 다시 받지 않기 위해서다. 가격 값은 변동 여부와 무관하게
    # 마지막 관측가와 같다.
    last_ts, last_price = observed[-1]
    await store.set_cursor(session, "upbit", base, last_ts, str(last_price))
    return len(changes)


async def sync_binance(session: AsyncSession, base: str, *, now_ts: int) -> int:
    """바이낸스 1초봉을 커서 이후부터 받아 변동만 스테이징에 쌓는다."""
    cursor = await store.get_cursor(session, "binance", base)
    if cursor is None:
        start_ts = now_ts - settings.history_sync_lookback_seconds
        seed: Decimal | None = None
    else:
        start_ts = cursor.last_ts + 1
        seed = Decimal(cursor.last_price)

    # 진행 중인 현재 초는 제외한다 — sync_upbit 의 주석 참고.
    observed = await binance_history.fetch_1s_range(base, start_ts, now_ts)
    if not observed:
        return 0

    changes = keep_changes(observed, seed)
    await store.add_price_points(
        session, "binance", base, [(ts, str(p)) for ts, p in changes]
    )
    last_ts, last_price = observed[-1]
    await store.set_cursor(session, "binance", base, last_ts, str(last_price))
    return len(changes)


async def record_fx_observation(
    session: AsyncSession, observation: hana.FxObservation
) -> bool:
    """고시 관측 한 건을 환율 이력에 반영한다 — refresh 와 sync 의 공용 경로.

    "변동만 저장" 원칙을 여기서 한 번만 구현한다:
        - 이미 반영한 고시(커서 이전)면 아무것도 하지 않는다.
        - 매매기준율이 직전 관측과 같으면 이벤트를 저장하지 않는다
          (커서만 전진 — 같은 구간을 다시 보지 않기 위해).
        - 라이브 계산용 단일 행(``fx_rate``)은 항상 최신으로 갱신한다.

    Returns:
        새 변동 이벤트를 저장했으면 True.
    """
    from app.db import repository  # 순환 import 방지를 위한 지역 import

    # 라이브 단일 행은 **staleness 와 무관하게 항상** 갱신을 시도한다.
    # (백필이 커서만 전진시키고 fx_rate 를 안 만든 상태에서, 주말처럼 새 고시가
    #  없으면 커서 검사만으로는 라이브 행이 영영 비는 버그가 있었다.)
    # 더 오래된 고시로의 역행은 upsert 자체의 WHERE 절이 막는다.
    await repository.upsert_fx_rate(
        session,
        rate=float(observation.rate),
        source_time=observation.ts,
        round_no=observation.round_no,
    )

    exchange, base = store.FX_CURSOR_KEY
    cursor = await store.get_cursor(session, exchange, base)
    if cursor is not None and observation.ts <= cursor.last_ts:
        return False  # 이미 본 고시 — 이력에는 새로 쌓을 것이 없다

    changed = cursor is None or Decimal(cursor.last_price) != observation.rate
    if changed:
        await store.add_fx_points(session, [(observation.ts, str(observation.rate))])
    await store.set_cursor(
        session, exchange, base, observation.ts, str(observation.rate)
    )
    return changed


async def sync_fx(session: AsyncSession) -> int:
    """하나은행 최신 고시 하나를 확인해 새 변동이면 스테이징에 쌓는다.

    고시는 평균 ~44초 간격이므로 1분 주기 sync 로도 대부분을 잡는다.
    (놓친 중간 회차는 환율 특성상 오차에 거의 기여하지 않는다 —
    빈틈없이 필요하면 백필 스크립트를 해당 날짜로 다시 돌리면 된다)
    """
    observation = await hana.fetch_latest()
    return 1 if await record_fx_observation(session, observation) else 0
