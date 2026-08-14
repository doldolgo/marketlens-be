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
from app.db.models import FxRate, MarketSnapshot
from app.models.orderbook import MarketType, OrderBook, OrderBookLevel


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


async def replace_exchange_snapshots(
    session: AsyncSession, exchange: str, rows: list[SnapshotRow]
) -> tuple[int, int]:
    """한 거래소의 스냅샷을 통째로 갱신한다.

    있는 행은 UPSERT 하고, 이번 수집에 없는 코인(상장폐지 등)은 지운다.
    호가는 초 단위로 낡으므로 남겨둘 이유가 없다.

    Returns:
        (저장한 행 수, 지운 행 수)
    """
    if rows:
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

    # 이번 수집에 없는 코인은 지운다. rows 가 비었으면 그 거래소 행 전체가 지워진다.
    keep = {r.base for r in rows}
    stmt = delete(MarketSnapshot).where(MarketSnapshot.exchange == exchange)
    if keep:
        stmt = stmt.where(MarketSnapshot.base.not_in(keep))
    result = await session.execute(stmt)
    return len(rows), result.rowcount or 0


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
    (refresh 와 /history/sync 가 동시에 돌 때의 역전 방지). 같은 고시 시각의
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
            "POST /refresh 또는 POST /history/sync 로 수집했는지 확인하세요.",
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
