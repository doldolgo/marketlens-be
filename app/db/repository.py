"""DB 읽기·쓰기의 단일 창구.

조회 API 들은 거래소를 직접 부르지 않고 전부 이 모듈을 통해 DB 를 읽는다.
쓰는 쪽은 수집기(:mod:`app.services.collector_service`) 하나뿐이다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import MarketDataNotFoundError
from app.db.models import FxRate, MarketSnapshot, PlatformStatus, PremiumArchive
from app.models.orderbook import MarketType, OrderBook, OrderBookLevel

#: 대량 INSERT 한 문장의 최대 행 수 — asyncpg 파라미터 한도(32,767개) 보호.
INSERT_BATCH = 5_000


@dataclass(slots=True)
class SnapshotRow:
    """수집기가 만들어 넘기는 스냅샷 한 행."""

    exchange: str
    base: str
    native_symbol: str
    quote: str
    price: float
    asks: list[list[float]] = field(default_factory=list)
    bids: list[list[float]] = field(default_factory=list)
    deposit_enabled: bool | None = None
    withdrawal_enabled: bool | None = None
    price_timestamp: int = 0


def _upsert(session: AsyncSession, table, rows: list[dict], index: list[str]):
    """방언에 맞는 UPSERT 문을 만든다 (PostgreSQL / SQLite 둘 다 지원)."""
    dialect = session.get_bind().dialect.name
    stmt = (pg_insert if dialect == "postgresql" else sqlite_insert)(table).values(rows)
    update_cols = {
        c.name: stmt.excluded[c.name]
        for c in table.__table__.columns
        if c.name not in index and c.name != "updated_at"
    }
    # onupdate 는 ORM update 에만 걸리므로, upsert 에서는 updated_at 을 직접 갱신한다.
    update_cols["updated_at"] = func.now()
    return stmt.on_conflict_do_update(index_elements=index, set_=update_cols)


# ----------------------------------------------------------------------
# 쓰기 — 수집기 전용
# ----------------------------------------------------------------------


async def upsert_exchange_snapshots(
    session: AsyncSession, exchange: str, rows: list[SnapshotRow]
) -> int:
    """한 거래소의 스냅샷을 **코인을 찾아 UPSERT** 한다.

    지웠다 다시 만들지 않는다 — 있는 행은 갱신, 없는 행은 삽입만 한다.
    이번 수집에 빠진 코인도 삭제하지 않는다 (일시 누락으로 데이터가
    사라지는 일을 막는다). 상장폐지 코인은 행이 남되 ``updated_at`` 이
    멈추므로 신선도 필드로 걸러진다.

    Returns:
        저장(UPSERT)한 행 수
    """
    if not rows:
        return 0
    payload = [
        {
            "exchange": r.exchange,
            "base": r.base,
            "native_symbol": r.native_symbol,
            "quote": r.quote,
            "price": r.price,
            "asks": r.asks,
            "bids": r.bids,
            "deposit_enabled": r.deposit_enabled,
            "withdrawal_enabled": r.withdrawal_enabled,
            "price_timestamp": r.price_timestamp,
        }
        for r in rows
    ]
    await session.execute(
        _upsert(session, MarketSnapshot, payload, ["exchange", "base"])
    )
    return len(rows)


async def upsert_fx_rate(
    session: AsyncSession,
    *,
    rate: float,
    source_time: int,
    round_no: int,
) -> None:
    """통일 환율(하나은행 USD/KRW 매매기준율) 단일 행을 갱신한다.

    예전의 거래소별 KRW-USDT 환율(``krw_rates``)을 대체한다 — 이제 모든
    원화 환산 계산이 이 한 행을 쓴다.

    UPSERT 의 WHERE 절로 **더 오래된 고시가 최신 값을 덮어쓰는 것을 막는다**
    (수집 경로가 겹칠 때의 역전 방지). 같은 고시 시각의
    재수신은 허용해 값이 항상 최소한 갱신 가능 상태를 유지한다.
    """
    dialect = session.get_bind().dialect.name
    stmt = (pg_insert if dialect == "postgresql" else sqlite_insert)(FxRate).values(
        [
            {
                "id": 1,
                "rate": rate,
                "source_time": source_time,
                "round_no": round_no,
            }
        ]
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["id"],
            set_={
                "rate": stmt.excluded.rate,
                "source_time": stmt.excluded.source_time,
                "round_no": stmt.excluded.round_no,
                "updated_at": func.now(),
            },
            where=FxRate.source_time <= stmt.excluded.source_time,
        )
    )


# ----------------------------------------------------------------------
# 김프/역프 아카이브 (기록/통계 창)
# ----------------------------------------------------------------------


async def add_premium_rows(
    session: AsyncSession,
    rows: list[dict],
) -> int:
    """아카이브 행들을 넣는다. 같은 (dom, fx, base, ts) 는 무시한다.

    refresh 의 실시간 추가와 대량 업데이트가 같은 시각에 겹쳐도 중복이
    생기지 않는다. rows 원소: {dom, fx, base, ts, fwd, rev}.

    Returns:
        **실제로 삽입된** 행 수 (중복으로 무시된 행은 세지 않는다).
    """
    dialect = session.get_bind().dialect.name
    insert = pg_insert if dialect == "postgresql" else sqlite_insert
    inserted = 0
    for i in range(0, len(rows), INSERT_BATCH):
        stmt = insert(PremiumArchive).values(rows[i : i + INSERT_BATCH])
        result = await session.execute(
            stmt.on_conflict_do_nothing(
                index_elements=["dom", "fx", "base", "ts"]
            )
        )
        inserted += result.rowcount or 0
    return inserted


async def get_premium_range(
    session: AsyncSession,
    dom: str,
    fx: str,
    base: str,
    start_ts: int,
    end_ts: int,
) -> list[PremiumArchive]:
    """[start_ts, end_ts) 구간의 아카이브 행을 시각 순으로."""
    result = await session.execute(
        select(PremiumArchive)
        .where(
            PremiumArchive.dom == dom,
            PremiumArchive.fx == fx,
            PremiumArchive.base == base,
            PremiumArchive.ts >= start_ts,
            PremiumArchive.ts < end_ts,
        )
        .order_by(PremiumArchive.ts)
    )
    return list(result.scalars())


async def get_premium_page(
    session: AsyncSession,
    dom: str,
    fx: str,
    base: str,
    start_ts: int,
    end_ts: int,
    offset: int,
    limit: int,
) -> list[PremiumArchive]:
    """구간 내 아카이브의 한 페이지 (시각 순, SQL LIMIT/OFFSET).

    ``offset > 0`` 이면 **직전 행(offset-1)도 함께** 반환한다 — 페이지 첫
    항목의 dt(직전 기록과의 시간 차)를 계산하려면 그 행의 시각이 필요하다.
    호출자는 첫 행을 기준점으로만 쓰고 응답에는 싣지 않는다.
    """
    fetch_offset = offset - 1 if offset > 0 else 0
    fetch_limit = limit + 1 if offset > 0 else limit
    result = await session.execute(
        select(PremiumArchive)
        .where(
            PremiumArchive.dom == dom,
            PremiumArchive.fx == fx,
            PremiumArchive.base == base,
            PremiumArchive.ts >= start_ts,
            PremiumArchive.ts < end_ts,
        )
        .order_by(PremiumArchive.ts)
        .offset(fetch_offset)
        .limit(fetch_limit)
    )
    return list(result.scalars())


async def get_premium_stats(
    session: AsyncSession,
    dom: str,
    fx: str,
    base: str,
    start_ts: int,
    end_ts: int,
) -> dict | None:
    """구간 요약 — 전체를 메모리에 올리지 않고 SQL 집계로 구한다.

    Returns:
        {count, first_ts, first_fwd, last_fwd, min_fwd, max_fwd},
        구간이 비었으면 None.
    """
    where = (
        PremiumArchive.dom == dom,
        PremiumArchive.fx == fx,
        PremiumArchive.base == base,
        PremiumArchive.ts >= start_ts,
        PremiumArchive.ts < end_ts,
    )
    agg = (
        await session.execute(
            select(
                func.count(PremiumArchive.ts),
                func.min(PremiumArchive.ts),
                func.min(PremiumArchive.fwd),
                func.max(PremiumArchive.fwd),
            ).where(*where)
        )
    ).one()
    count, first_ts, min_fwd, max_fwd = agg
    if not count:
        return None

    first_row = (
        await session.execute(
            select(PremiumArchive.fwd).where(*where).order_by(PremiumArchive.ts).limit(1)
        )
    ).scalar_one()
    last_row = (
        await session.execute(
            select(PremiumArchive.fwd)
            .where(*where)
            .order_by(PremiumArchive.ts.desc())
            .limit(1)
        )
    ).scalar_one()
    return {
        "count": int(count),
        "first_ts": int(first_ts),
        "first_fwd": first_row,
        "last_fwd": last_row,
        "min_fwd": min_fwd,
        "max_fwd": max_fwd,
    }


async def get_premium_bounds(
    session: AsyncSession, dom: str, fx: str, base: str
) -> tuple[int, int] | None:
    """한 시리즈의 (첫 시각, 마지막 시각). 비어 있으면 None.

    대량 업데이트가 "아카이브에 없는 시간대 구간"을 판단하는 기준이다.
    """
    result = await session.execute(
        select(
            func.min(PremiumArchive.ts), func.max(PremiumArchive.ts)
        ).where(
            PremiumArchive.dom == dom,
            PremiumArchive.fx == fx,
            PremiumArchive.base == base,
        )
    )
    first, last = result.one()
    if first is None:
        return None
    return int(first), int(last)


# ----------------------------------------------------------------------
# 플랫폼 상태
# ----------------------------------------------------------------------


async def bump_platform_status(
    session: AsyncSession,
    *,
    exchange: str,
    received_ts: int,
    spot_market_count: int,
    futures_market_count: int | None,
    dw_failed: bool,
) -> None:
    """플랫폼 행을 갱신한다 — 수신 시각 기록 + 카운터 증가.

    market_snapshots 업데이트 직후 호출된다:
        - last_received_ts ← 이번 수신 시각, update_count += 1
        - 이번 업데이트에서 입금 또는 출금 불가 코인이 있었으면(dw_failed)
          dw_fail_count += 1
    실패율 = dw_fail_count / update_count 로 계산한다.
    """
    dialect = session.get_bind().dialect.name
    insert = pg_insert if dialect == "postgresql" else sqlite_insert
    stmt = insert(PlatformStatus).values(
        [
            {
                "exchange": exchange,
                "last_received_ts": received_ts,
                "spot_market_count": spot_market_count,
                "futures_market_count": futures_market_count or 0,
                "dw_fail_count": 1 if dw_failed else 0,
                "update_count": 1,
            }
        ]
    )
    set_ = {
        "last_received_ts": stmt.excluded.last_received_ts,
        "spot_market_count": stmt.excluded.spot_market_count,
        # 카운터는 기존 값에 누적한다 (덮어쓰기가 아니라 +1).
        "dw_fail_count": PlatformStatus.dw_fail_count + stmt.excluded.dw_fail_count,
        "update_count": PlatformStatus.update_count + 1,
        "updated_at": func.now(),
    }
    # None 은 "이번에 못 셌다" — 이전 값을 유지한다 (0 으로 덮지 않는다).
    if futures_market_count is not None:
        set_["futures_market_count"] = stmt.excluded.futures_market_count
    await session.execute(
        stmt.on_conflict_do_update(index_elements=["exchange"], set_=set_)
    )


async def get_platform_statuses(session: AsyncSession) -> list[PlatformStatus]:
    """모든 플랫폼의 상태 행."""
    result = await session.execute(
        select(PlatformStatus).order_by(PlatformStatus.exchange)
    )
    return list(result.scalars())


# ----------------------------------------------------------------------
# 읽기 — 조회 API 공용
# ----------------------------------------------------------------------


async def get_snapshots(
    session: AsyncSession,
    *,
    exchange: str | None = None,
    base: str | None = None,
) -> list[MarketSnapshot]:
    """스냅샷을 조건으로 조회한다. 조건이 없으면 전체."""
    stmt = select(MarketSnapshot)
    if exchange is not None:
        stmt = stmt.where(MarketSnapshot.exchange == exchange)
    if base is not None:
        stmt = stmt.where(MarketSnapshot.base == base.upper())
    result = await session.execute(stmt)
    return list(result.scalars())


async def get_snapshot(
    session: AsyncSession, exchange: str, base: str
) -> MarketSnapshot | None:
    """거래소 × 코인 하나의 스냅샷."""
    return await session.get(MarketSnapshot, (exchange, base.upper()))


async def require_snapshot(
    session: AsyncSession, exchange: str, base: str
) -> MarketSnapshot:
    """스냅샷을 가져오되, 없으면 404 성격의 도메인 예외를 던진다."""
    snap = await get_snapshot(session, exchange, base)
    if snap is None:
        raise MarketDataNotFoundError(
            f"DB 에 {exchange} 거래소의 {base.upper()} 스냅샷이 없습니다. "
            "POST /refresh 로 데이터를 수집했는지, 해당 거래소에 상장된 코인인지 "
            "확인하세요.",
            detail={"exchange": exchange, "base": base.upper()},
        )
    return snap


async def get_fx_rate(session: AsyncSession) -> FxRate | None:
    """통일 환율(USD/KRW 매매기준율). 아직 수집 전이면 None."""
    return await session.get(FxRate, 1)


async def require_fx_rate(session: AsyncSession) -> FxRate:
    """통일 환율을 가져오되, 없거나 0 이하면 도메인 예외를 던진다."""
    rate = await get_fx_rate(session)
    if rate is None or rate.rate <= 0:
        raise MarketDataNotFoundError(
            "DB 에 USD/KRW 환율이 없습니다. "
            "POST /refresh 로 수집했는지 확인하세요.",
        )
    return rate


# ----------------------------------------------------------------------
# 변환 헬퍼
# ----------------------------------------------------------------------


def levels_from_json(raw: list) -> list[OrderBookLevel]:
    """DB 의 [[가격, 잔량], ...] 를 OrderBookLevel 리스트로 바꾼다."""
    return [OrderBookLevel(price=float(p), size=float(s)) for p, s in raw]


def orderbook_from_snapshot(
    snap: MarketSnapshot, *, depth: int | None = None
) -> OrderBook:
    """스냅샷 한 행을 기존 OrderBook 모델로 되살린다.

    조회 API 들이 기존 계산 로직(orderbook_walk 등)을 그대로 재사용할 수 있다.
    """
    asks = levels_from_json(snap.asks)
    bids = levels_from_json(snap.bids)
    if depth is not None:
        asks = asks[:depth]
        bids = bids[:depth]
    return OrderBook(
        exchange=snap.exchange,
        symbol=f"{snap.base}/{snap.quote}",
        native_symbol=snap.native_symbol,
        market_type=MarketType.SPOT,
        base=snap.base,
        quote=snap.quote,
        bids=bids,
        asks=asks,
        timestamp=snap.price_timestamp,
        latency_ms=0.0,
        data_updated_at=(
            int(snap.updated_at.timestamp() * 1000)
            if snap.updated_at is not None
            else None
        ),
    )
