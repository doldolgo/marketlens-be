"""비동기 DB 엔진·세션 관리.

이 백엔드의 데이터 흐름은 두 갈래다.

    수집:  POST /refresh  → 거래소 API 호출 → DB 에 저장
    조회:  그 외 모든 API → DB 에서 읽어서 계산 (거래소 직접 호출 없음)

엔진은 프로세스당 하나를 공유하고, 요청마다 세션을 새로 연다.
테이블은 앱 기동 시 ``init_db`` 가 없으면 만들어준다 (CREATE TABLE IF NOT EXISTS).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings
from app.db.models import Base

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _async_url(url: str) -> str:
    """``postgresql://`` URL 을 asyncpg 드라이버 URL 로 바꾼다.

    .env 에는 드라이버를 모르는 표준 URL 을 두고, 코드가 알아서 맞춘다.
    """
    if url.startswith("postgresql+asyncpg://"):
        return url
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    return url


def get_engine() -> AsyncEngine:
    """공용 엔진을 반환한다. 없으면 지연 생성한다."""
    global _engine, _session_factory
    if _engine is None:
        _engine = create_async_engine(
            _async_url(settings.database_url),
            pool_size=10,
            max_overflow=10,
            pool_pre_ping=True,
        )
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """세션 팩토리를 반환한다."""
    get_engine()
    assert _session_factory is not None
    return _session_factory


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 의존성 — 요청 하나에 세션 하나."""
    async with get_session_factory()() as session:
        yield session


async def init_db() -> None:
    """테이블이 없으면 만들고, 사람이 읽는 뷰를 갱신한다. 앱 기동 시 호출된다."""
    engine = get_engine()
    async with engine.begin() as conn:
        # 테이블 **모양**이 바뀐 경우의 정리는 create_all 보다 먼저 해야 한다 —
        # create_all 은 "없는 테이블만 만들기"라 옛 모양이 남아 있으면 그대로 둔다.
        if conn.dialect.name == "postgresql":
            from sqlalchemy import text

            from app.db.views import PRE_CREATE_DDL

            for ddl in PRE_CREATE_DDL:
                await conn.execute(text(ddl))

        await conn.run_sync(Base.metadata.create_all)
        # 읽기 전용 뷰 — epoch 초를 연도 포함 KST 시각으로 보여준다.
        # 이전 구조(압축 이력·krw_rates)의 잔재도 이때 정리한다.
        # PostgreSQL 전용 (테스트용 SQLite 에는 만들지 않는다).
        if conn.dialect.name == "postgresql":
            from sqlalchemy import text

            from app.db.views import CLEANUP_DDL, VIEW_DDL

            for ddl in (*CLEANUP_DDL, *VIEW_DDL):
                await conn.execute(text(ddl))


async def dispose_engine() -> None:
    """앱 종료 시 커넥션 풀을 정리한다."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
