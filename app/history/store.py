"""이력 테이블 읽기·쓰기의 단일 창구.

:mod:`app.db.repository` 가 라이브 테이블(스냅샷·환율)의 창구이듯,
이 모듈은 이력 테이블(청크·스테이징·커서)의 창구다.

쓰기 규칙
    - 스테이징(``price_points`` / ``fx_points``)은 INSERT ... ON CONFLICT
      **DO NOTHING** — 수집이 겹쳐도(백필과 주기 수집 동시 실행 등)
      같은 (시각) 이벤트가 중복 저장되지 않는다.
    - 청크는 UPSERT — 팩킹을 다시 돌려도 같은 날짜에 안전하다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import FxChunk, FxPoint, HistoryCursor, PriceChunk, PricePoint

#: 환율 시리즈가 history_cursors 에서 쓰는 키. 거래소 개념이 없어 고정값이다.
FX_CURSOR_KEY = ("fx", "USD")

#: INSERT 한 문장에 넣는 최대 행 수. asyncpg 는 쿼리 파라미터가 32,767개를
#: 넘으면 실패한다 — 행당 4개 파라미터 기준 5,000행 = 20,000개로 여유를 둔다.
#: (백필이 하루 3만~4만 건을 한 번에 넣다가 실제로 터진 한도다)
INSERT_BATCH = 5_000


@dataclass(slots=True)
class ChunkData:
    """팩킹이 만들어 넘기는 청크 한 행 분량의 값 묶음."""

    day: date
    codec: int
    price_scale: int
    n_points: int
    first_ts: int
    last_ts: int
    first_price: int
    last_price: int
    min_price: int
    max_price: int
    data: bytes


def _insert_ignore(session: AsyncSession, table, rows: list[dict], index: list[str]):
    """방언에 맞는 INSERT ... ON CONFLICT DO NOTHING (PostgreSQL / SQLite)."""
    dialect = session.get_bind().dialect.name
    stmt = (pg_insert if dialect == "postgresql" else sqlite_insert)(table).values(
        rows
    )
    return stmt.on_conflict_do_nothing(index_elements=index)


def _upsert(session: AsyncSession, table, rows: list[dict], index: list[str]):
    """방언에 맞는 UPSERT."""
    dialect = session.get_bind().dialect.name
    stmt = (pg_insert if dialect == "postgresql" else sqlite_insert)(table).values(
        rows
    )
    update_cols = {
        c.name: stmt.excluded[c.name]
        for c in table.__table__.columns
        if c.name not in index and c.name != "updated_at"
    }
    return stmt.on_conflict_do_update(index_elements=index, set_=update_cols)


# ----------------------------------------------------------------------
# 코인 가격 시리즈
# ----------------------------------------------------------------------


async def add_price_points(
    session: AsyncSession,
    exchange: str,
    base: str,
    points: list[tuple[int, str]],
) -> None:
    """변동 이벤트(epoch 초, 가격 문자열)를 스테이징에 넣는다. 중복은 무시.

    파라미터 한도를 넘지 않도록 INSERT_BATCH 행씩 나눠 넣는다 —
    백필이 하루치 수만 건을 한 번에 부를 수 있다.
    """
    rows = [
        {"exchange": exchange, "base": base, "ts": ts, "price": price}
        for ts, price in points
    ]
    for i in range(0, len(rows), INSERT_BATCH):
        await session.execute(
            _insert_ignore(
                session,
                PricePoint,
                rows[i : i + INSERT_BATCH],
                ["exchange", "base", "ts"],
            )
        )


async def get_price_points(
    session: AsyncSession,
    exchange: str,
    base: str,
    start_ts: int,
    end_ts: int,
) -> list[tuple[int, str]]:
    """스테이징에서 [start_ts, end_ts) 구간의 이벤트를 시각 순으로."""
    result = await session.execute(
        select(PricePoint.ts, PricePoint.price)
        .where(
            PricePoint.exchange == exchange,
            PricePoint.base == base,
            PricePoint.ts >= start_ts,
            PricePoint.ts < end_ts,
        )
        .order_by(PricePoint.ts)
    )
    return [(row.ts, row.price) for row in result]


async def delete_price_points(
    session: AsyncSession, exchange: str, base: str, ts_list: list[int]
) -> int:
    """팩킹한 바로 그 이벤트들만 스테이징에서 지운다.

    "cutoff 이전 전부 삭제" 방식은 SELECT 와 DELETE 사이에 다른 트랜잭션이
    커밋한 (아직 팩킹 안 된) 행까지 지워버릴 수 있다 — READ COMMITTED 에서
    DELETE 는 새 스냅샷을 보기 때문이다. 그래서 반드시 **팩킹에 실제로 포함한
    타임스탬프 목록**으로만 지운다.
    """
    deleted = 0
    for i in range(0, len(ts_list), 5_000):  # IN 절이 무한정 길어지지 않게 분할
        result = await session.execute(
            delete(PricePoint).where(
                PricePoint.exchange == exchange,
                PricePoint.base == base,
                PricePoint.ts.in_(ts_list[i : i + 5_000]),
            )
        )
        deleted += result.rowcount or 0
    return deleted


async def upsert_price_chunk(
    session: AsyncSession, exchange: str, base: str, chunk: ChunkData
) -> None:
    """하루치 압축 청크를 저장한다 (재팩킹에 안전한 UPSERT)."""
    await session.execute(
        _upsert(
            session,
            PriceChunk,
            [
                {
                    "exchange": exchange,
                    "base": base,
                    "day": chunk.day,
                    "codec": chunk.codec,
                    "price_scale": chunk.price_scale,
                    "n_points": chunk.n_points,
                    "first_ts": chunk.first_ts,
                    "last_ts": chunk.last_ts,
                    "first_price": chunk.first_price,
                    "last_price": chunk.last_price,
                    "min_price": chunk.min_price,
                    "max_price": chunk.max_price,
                    "data": chunk.data,
                }
            ],
            ["exchange", "base", "day"],
        )
    )


async def get_price_chunks(
    session: AsyncSession,
    exchange: str,
    base: str,
    first_day: date,
    last_day: date,
) -> list[PriceChunk]:
    """[first_day, last_day] (양끝 포함) 범위의 청크를 날짜 순으로."""
    result = await session.execute(
        select(PriceChunk)
        .where(
            PriceChunk.exchange == exchange,
            PriceChunk.base == base,
            PriceChunk.day >= first_day,
            PriceChunk.day <= last_day,
        )
        .order_by(PriceChunk.day)
    )
    return list(result.scalars())


async def get_price_chunk_days(
    session: AsyncSession, exchange: str, base: str
) -> set[date]:
    """이미 청크가 있는 날짜 집합 — 백필 재개 시 건너뛸 날을 판단한다."""
    result = await session.execute(
        select(PriceChunk.day).where(
            PriceChunk.exchange == exchange, PriceChunk.base == base
        )
    )
    return {row.day for row in result}


# ----------------------------------------------------------------------
# 환율 시리즈 (구조 동일, 테이블만 다름)
# ----------------------------------------------------------------------


async def add_fx_points(
    session: AsyncSession, points: list[tuple[int, str]]
) -> None:
    """환율 변동 이벤트를 스테이징에 넣는다. 중복(같은 고시 시각)은 무시."""
    rows = [{"ts": ts, "price": price} for ts, price in points]
    for i in range(0, len(rows), INSERT_BATCH):
        await session.execute(
            _insert_ignore(session, FxPoint, rows[i : i + INSERT_BATCH], ["ts"])
        )


async def get_fx_points(
    session: AsyncSession, start_ts: int, end_ts: int
) -> list[tuple[int, str]]:
    result = await session.execute(
        select(FxPoint.ts, FxPoint.price)
        .where(FxPoint.ts >= start_ts, FxPoint.ts < end_ts)
        .order_by(FxPoint.ts)
    )
    return [(row.ts, row.price) for row in result]


async def delete_fx_points(session: AsyncSession, ts_list: list[int]) -> int:
    """팩킹한 환율 이벤트들만 지운다 — 이유는 delete_price_points 와 같다."""
    deleted = 0
    for i in range(0, len(ts_list), 5_000):
        result = await session.execute(
            delete(FxPoint).where(FxPoint.ts.in_(ts_list[i : i + 5_000]))
        )
        deleted += result.rowcount or 0
    return deleted


async def upsert_fx_chunk(session: AsyncSession, chunk: ChunkData) -> None:
    await session.execute(
        _upsert(
            session,
            FxChunk,
            [
                {
                    "day": chunk.day,
                    "codec": chunk.codec,
                    "price_scale": chunk.price_scale,
                    "n_points": chunk.n_points,
                    "first_ts": chunk.first_ts,
                    "last_ts": chunk.last_ts,
                    "first_price": chunk.first_price,
                    "last_price": chunk.last_price,
                    "min_price": chunk.min_price,
                    "max_price": chunk.max_price,
                    "data": chunk.data,
                }
            ],
            ["day"],
        )
    )


async def get_fx_chunks(
    session: AsyncSession, first_day: date, last_day: date
) -> list[FxChunk]:
    result = await session.execute(
        select(FxChunk)
        .where(FxChunk.day >= first_day, FxChunk.day <= last_day)
        .order_by(FxChunk.day)
    )
    return list(result.scalars())


async def get_fx_chunk_days(session: AsyncSession) -> set[date]:
    result = await session.execute(select(FxChunk.day))
    return {row.day for row in result}


# ----------------------------------------------------------------------
# 수집 커서
# ----------------------------------------------------------------------


async def get_cursor(
    session: AsyncSession, exchange: str, base: str
) -> HistoryCursor | None:
    """시리즈의 증분 수집 커서. 없으면(첫 수집 전) None."""
    return await session.get(HistoryCursor, (exchange, base))


async def set_cursor(
    session: AsyncSession, exchange: str, base: str, last_ts: int, last_price: str
) -> None:
    """커서를 전진시킨다.

    UPSERT 의 WHERE 절로 **뒤로 가는 갱신을 DB 차원에서 막는다** —
    refresh 와 sync 가 동시에 돌다 낡은 관측이 나중에 커밋돼도
    커서가 과거로 되돌아가지 않는다.
    """
    dialect = session.get_bind().dialect.name
    stmt = (pg_insert if dialect == "postgresql" else sqlite_insert)(
        HistoryCursor
    ).values(
        [
            {
                "exchange": exchange,
                "base": base,
                "last_ts": last_ts,
                "last_price": last_price,
            }
        ]
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["exchange", "base"],
            set_={
                "last_ts": stmt.excluded.last_ts,
                "last_price": stmt.excluded.last_price,
            },
            where=HistoryCursor.last_ts < stmt.excluded.last_ts,
        )
    )
