"""김프/역프 아카이브 서비스 — 계산·기록·대량 채우기.

기록/통계 창의 데이터(``premium_archive``)를 만드는 두 경로의 공용 로직이다.

    실시간 (POST /refresh)
        market_snapshots 를 업데이트한 직후, 스냅샷의 **체결측 호가**와 통일
        환율로 김프/역프를 계산해 아카이브에 한 줄 추가한다 (/spreads 와 동일식).

    대량 업데이트 (scripts/bulk_archive.py)
        아카이브의 첫/마지막 시각 **밖의 구간**을 거래소 캔들(업비트 초봉 ·
        바이낸스 1초봉 · 환율은 업비트 KRW-USDT 분봉)로 한 번에 채운다.
        캔들에는 호가가 없으므로 **종가 기준**으로 계산한다 — 그래서 실시간
        기록과 달리 방향별 환율(ask/bid)을 복원할 수 없고 fwd/rev 가 대칭이다.

시각은 절대 epoch 초로 저장하고, 조회 API 가 상대 시간(직전 기록에서 몇 초
뒤)으로 변환한다.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import repository
from app.history import binance as binance_history
from app.history import upbit as upbit_history

# ----------------------------------------------------------------------
# 김프/역프 계산식 — 실시간(호가)과 대량(종가) 두 가지
# ----------------------------------------------------------------------


def premium_from_quotes(
    dom_bid: float,
    dom_ask: float,
    fx_bid: float,
    fx_ask: float,
    fx_ask_rate: float,
    fx_bid_rate: float,
) -> tuple[float, float] | None:
    """체결측 호가 기준 (김프 %, 역프 %) — /spreads · /premium 과 동일한 공식.

    김프(해외 매수→국내 매도): 원화로 USDT 를 **사서**(환율 ask) 해외 ask 로
    코인을 사고 국내 bid 에 판다.
    역프(국내 매수→해외 매도): 국내 ask 로 사서 해외 bid 에 팔고, 받은 USDT 를
    원화로 **판다**(환율 bid).

    환율도 체결되는 쪽 호가를 쓰므로 방향마다 다른 값이 들어간다.
    값이 하나라도 없거나 0 이하면 None (계산 불가).
    """
    if min(dom_bid, dom_ask, fx_bid, fx_ask, fx_ask_rate, fx_bid_rate) <= 0:
        return None
    fwd = (dom_bid / (fx_ask * fx_ask_rate) - 1) * 100
    rev = (fx_bid * fx_bid_rate / dom_ask - 1) * 100
    return fwd, rev


def premium_from_closes(
    dom_price: float, fx_price: float, rate: float
) -> tuple[float, float] | None:
    """종가 기준 (김프 %, 역프 %) — 캔들로 채우는 대량 업데이트용.

    캔들에는 호가가 없어 양방향 모두 같은 종가를 쓴다 — **환율도 마찬가지로**
    KRW-USDT 종가 하나다 (과거 ask1/bid1 은 복원할 방법이 없다). 그래서 실시간
    기록과 달리 fwd 와 rev 가 정확히 대칭이다 (rev = 역수 관계).
    """
    if min(dom_price, fx_price, rate) <= 0:
        return None
    ratio = dom_price / (fx_price * rate)
    return (ratio - 1) * 100, (1 / ratio - 1) * 100


# ----------------------------------------------------------------------
# 주 / 월 구간 계산 (조회 API 용)
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


# ----------------------------------------------------------------------
# 대량 업데이트 — 아카이브 밖 시간대 구간을 캔들로 채운다
# ----------------------------------------------------------------------


def missing_ranges(
    bounds: tuple[int, int] | None, target_start: int, target_end: int
) -> list[tuple[int, int]]:
    """아카이브의 (첫, 마지막) 시각을 기준으로 채워야 할 구간을 계산한다.

    아카이브가 비었으면 목표 구간 전체, 아니면 첫 시각 **이전**과 마지막 시각
    **이후** 두 구간이다 (기록이 있는 가운데 구간은 건드리지 않는다).
    """
    if bounds is None:
        return [(target_start, target_end)] if target_start < target_end else []
    first, last = bounds
    ranges = []
    if target_start < first:
        ranges.append((target_start, first))
    if last + 1 < target_end:
        ranges.append((last + 1, target_end))
    return ranges


def _changes(
    points: list[tuple[int, Decimal]], seed: Decimal | None
) -> list[tuple[int, Decimal]]:
    """직전 값과 같은 포인트를 버린다 — 변동된 순간만 남긴다."""
    out: list[tuple[int, Decimal]] = []
    prev = seed
    for ts, value in points:
        if prev is None or value != prev:
            out.append((ts, value))
            prev = value
    return out


def merge_premium_timeline(
    upbit_events: list[tuple[int, Decimal]],
    binance_events: list[tuple[int, Decimal]],
    usdkrw_events: list[tuple[int, Decimal]],
    *,
    seeds: tuple[Decimal | None, Decimal | None, Decimal | None] = (None, None, None),
) -> tuple[list[dict], tuple[Decimal | None, Decimal | None, Decimal | None]]:
    """세 변동 시계열을 시각 순으로 걸으며 김프/역프 행을 만든다.

    각 입력은 (epoch 초, 값) 오름차순의 "변동된 순간" 목록이다. 어느 하나라도
    변한 초마다, 그 시점에 유효한 세 값(직전 값 유지 — forward fill)으로
    종가 기준 김프를 계산한다. 세 값이 모두 갖춰지기 전의 초는 건너뛴다.

    Returns:
        (아카이브 행 목록(dom/fx/base 는 호출자가 채움), 마지막 세 값 —
        다음 날 이어서 계산할 때 씨앗으로 쓴다)
    """
    up, bn, usdkrw = seeds
    merged: dict[int, list] = {}
    for ts, v in upbit_events:
        merged.setdefault(ts, [None, None, None])[0] = v
    for ts, v in binance_events:
        merged.setdefault(ts, [None, None, None])[1] = v
    for ts, v in usdkrw_events:
        merged.setdefault(ts, [None, None, None])[2] = v

    rows: list[dict] = []
    # 씨앗이 완전하면 "직전 김프"도 씨앗으로 계산한다 — 날짜 경계에서
    # 전날 마지막 값과 같은 김프가 중복 기록되는 것을 막는다.
    prev_fwd: float | None = None
    if up is not None and bn is not None and usdkrw is not None:
        seeded = premium_from_closes(float(up), float(bn), float(usdkrw))
        if seeded is not None:
            prev_fwd = seeded[0]
    for ts in sorted(merged):
        u_new, b_new, f_new = merged[ts]
        up = u_new if u_new is not None else up
        bn = b_new if b_new is not None else bn
        usdkrw = f_new if f_new is not None else usdkrw
        if up is None or bn is None or usdkrw is None:
            continue  # 아직 세 값이 다 갖춰지지 않은 초반 구간
        result = premium_from_closes(float(up), float(bn), float(usdkrw))
        if result is None:
            continue
        fwd, rev = result
        if prev_fwd is not None and fwd == prev_fwd:
            continue  # 김프가 그대로면 기록할 것이 없다
        prev_fwd = fwd
        rows.append({"ts": ts, "fwd": fwd, "rev": rev})
    return rows, (up, bn, usdkrw)


async def fill_premium_gap(
    session: AsyncSession,
    base: str,
    start_ts: int,
    end_ts: int,
    *,
    usdkrw_events: list[tuple[int, Decimal]],
    newest_first: bool = False,
    pace_upbit: float = 0.2,
    pace_binance: float = 0.1,
    log=lambda msg: None,
) -> int:
    """[start_ts, end_ts) 구간의 (upbit × binance) 김프 기록을 캔들로 채운다.

    **하루(UTC) 단위로 독립 처리**한다 — 각 날은 그 날의 관측만으로 계산하고
    (씨앗은 환율만: 그 날 시작 이전 마지막 고시), 날마다 커밋한다. 그래서
    어느 날에서 중단되든 "완성된 날"과 "아예 없는 날"만 남는다.

    ``newest_first=True`` 는 **기존 기록 이전(head) 구간**을 채울 때 쓴다 —
    최신 날부터 거꾸로 진행하면 중단돼도 남은 미완 구간이 항상 아카이브
    첫 시각 **밖**에 있어, 재실행 시 missing_ranges 가 다시 잡아낸다.
    (시간순으로 채우면 남은 구간이 첫/마지막 시각 사이에 갇혀 영영 안 채워진다)

    트레이드오프: 날 독립 처리라 각 날 첫 몇 초는 업비트 첫 체결이 나올
    때까지 계산이 시작되지 않고, 날 경계에서 직전 날 마지막과 같은 값의
    행이 하나 더 생길 수 있다 — 기록의 정확성에는 영향이 없다.

    Args:
        usdkrw_events: 구간을 덮는 환율 변동 목록 (호출자가 미리 수집.
            구간 시작 이전 씨앗 값 포함).
    Returns:
        저장한 행 수.
    """
    # 하루 경계로 자른 (시작, 끝) 목록.
    day_ranges: list[tuple[int, int]] = []
    cursor = start_ts
    while cursor < end_ts:
        day_end = min(end_ts, (cursor // 86_400 + 1) * 86_400)
        day_ranges.append((cursor, day_end))
        cursor = day_end
    if newest_first:
        day_ranges.reverse()

    saved = 0
    for day_start, day_end in day_ranges:
        upbit_obs = await upbit_history.fetch_seconds_range(
            base, day_start, day_end, pace=pace_upbit
        )
        binance_obs = await binance_history.fetch_1s_range(
            base, day_start, day_end, pace=pace_binance
        )
        # 환율 씨앗: 이 날 시작 이전의 마지막 고시.
        usdkrw_seed: Decimal | None = None
        for ts, v in usdkrw_events:
            if ts <= day_start:
                usdkrw_seed = v
            else:
                break
        upbit_ev = _changes(upbit_obs, None)
        binance_ev = _changes(binance_obs, None)
        usdkrw_ev = [(ts, v) for ts, v in usdkrw_events if day_start < ts < day_end]

        rows, _ = merge_premium_timeline(
            upbit_ev, binance_ev, usdkrw_ev, seeds=(None, None, usdkrw_seed)
        )

        if rows:
            await repository.add_premium_rows(
                session,
                [
                    {"dom": "upbit", "fx": "binance", "base": base, **row}
                    for row in rows
                ],
            )
            await session.commit()
            saved += len(rows)
        day = datetime.fromtimestamp(day_start, tz=timezone.utc).date()
        log(
            f"upbit×binance:{base} {day} — 업비트 {len(upbit_obs):,} · "
            f"바이낸스 {len(binance_obs):,} 관측 → 김프 기록 {len(rows):,}건"
        )
    return saved


#: 환율 씨앗을 찾기 위해 구간 시작보다 얼마나 더 과거까지 받을지 (초).
#: 첫 캔들이 start_ts 보다 뒤일 수 있어 그 앞의 마지막 종가가 필요하다.
USDT_SEED_LOOKBACK = 6 * 3600


async def collect_usdkrw_events(
    start_ts: int,
    end_ts: int,
    *,
    pace: float = 0.2,
    log=lambda msg: None,
) -> list[tuple[int, Decimal]]:
    """[start_ts, end_ts) 구간을 덮는 KRW-USDT 환율 변동 목록.

    업비트 ``KRW-USDT`` **1분봉 종가**로 만든다. 초봉이 아닌 이유는 요청 수다 —
    같은 구간을 1/60 의 호출로 덮을 수 있고, USDT/KRW 는 분 안에서 거의 움직이지
    않아 오차가 수 bp 수준이다. (라이브 경로는 초 단위 호가를 그대로 쓴다)

    구간 직전 값도 포함해 반환하므로 첫 초부터 환율이 유효하다. 값이 바뀐
    시점만 남긴다 — 호출부(merge_premium_timeline)가 forward-fill 한다.
    """
    observations = await upbit_history.fetch_minutes_range(
        "USDT", start_ts - USDT_SEED_LOOKBACK, end_ts, pace=pace
    )
    if not observations:
        return []

    events = _changes(observations, None)
    log(f"usdkrw — KRW-USDT 분봉 {len(observations):,}건 → 변동 {len(events):,}건")
    return events
