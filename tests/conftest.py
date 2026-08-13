"""공용 테스트 픽스처 — in-memory SQLite + FastAPI 세션 주입.

네트워크 호출은 어디에도 없다. 조회 API 는 전부 DB 만 읽으므로,
시나리오 데이터를 SQLite 에 심어두고 서비스/라우트를 **실제 코드**로 태운다.

- ``engine`` / ``db``     : in-memory SQLite (StaticPool 로 커넥션 하나를 공유)
- ``client``              : httpx.ASGITransport + dependency_overrides[get_session]
                            (lifespan 을 돌리지 않으므로 실제 DB 접속 시도가 없다)
- ``seed_standard``       : 국내 2곳(업비트·빗썸) + 바이낸스 + 통일 환율 표준 시나리오
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.db import repository
from app.db.models import Base
from app.db.repository import SnapshotRow

NOW_MS = 1_700_000_000_000


@pytest.fixture(autouse=True)
def _stable_settings(monkeypatch):
    """개발자의 .env 가 테스트 결과를 흔들지 않도록 설정을 기본값으로 고정한다.

    settings 는 import 시점에 .env 를 읽는다. 예컨대 .env 에
    ``SCAN_EXCLUDED_BASES=["AI","PROS"]`` 가 있으면 AI 를 테스트 코인으로 쓰는
    케이스가 통째로 걸러져 실패한다 — 테스트는 항상 같은 조건에서 돌아야 한다.
    """
    monkeypatch.setattr(settings, "scan_excluded_bases", [])
    monkeypatch.setattr(settings, "scan_suspicious_percent", 5.0)
    monkeypatch.setattr(settings, "krw_reference_exchange", "upbit")
    monkeypatch.setattr(settings, "krw_reference_quote", "KRW")
    monkeypatch.setattr(settings, "fx_stablecoin", "USDT")
    monkeypatch.setattr(settings, "orderbook_max_amount_krw", 1_000_000_000.0)
    monkeypatch.setattr(settings, "default_orderbook_depth", 10)

# ── 표준 시나리오 시세 ──────────────────────────────────────────────
# 환율 1,400 기준 바이낸스 BTC 71,000 USDT = 99,400,000원 → 김프 +0.6% 안팎.
UPBIT_PRICES = {"BTC": 100_000_000.0, "ETH": 5_000_000.0, "XRP": 1_400.0}
BITHUMB_PRICES = {"BTC": 100_100_000.0, "XRP": 1_402.0}
BINANCE_PRICES = {"BTC": 71_000.0, "ETH": 3_550.0, "XRP": 0.99, "SOL": 150.0}
#: 통일 환율 (하나은행 고시 USD/KRW 매매기준율) — 모든 계산이 이 하나를 쓴다.
FX_RATE = 1400.0

#: 호가 한 단계의 체결 가능 금액 (원화 환산). 슬리피지 기대값 계산이 쉽도록 고정.
LEVEL_AMOUNT_KRW = 3_000_000.0
BOOK_LEVELS = 5
#: 1단계부터 ±0.05% 스프레드, 단계마다 0.05% 씩 더 벌어진다.
BOOK_STEP = 0.0005


def make_levels(
    price: float,
    *,
    krw_factor: float = 1.0,
    level_amount: float = LEVEL_AMOUNT_KRW,
    levels: int = BOOK_LEVELS,
    step: float = BOOK_STEP,
) -> tuple[list[list[float]], list[list[float]]]:
    """(asks 오름차순, bids 내림차순) JSON 호가를 만든다.

    ``krw_factor`` 는 원화 환산 계수 — USDT 마켓이면 환율을 넘겨서
    한 단계의 체결 가능 금액이 원화 기준 ``level_amount`` 가 되게 한다.
    """
    size = level_amount / (price * krw_factor)
    asks = [[price * (1 + step * (i + 1)), size] for i in range(levels)]
    bids = [[price * (1 - step * (i + 1)), size] for i in range(levels)]
    return asks, bids


def snapshot_row(
    exchange: str,
    base: str,
    price: float,
    *,
    quote: str = "KRW",
    krw_factor: float = 1.0,
    deposit: bool | None = True,
    withdrawal: bool | None = True,
    native: str | None = None,
    ts: int = NOW_MS,
) -> SnapshotRow:
    """표준 모양(5단계, 단계당 300만원어치) 호가를 가진 스냅샷 한 행."""
    asks, bids = make_levels(price, krw_factor=krw_factor)
    if native is None:
        native = f"KRW-{base}" if quote == "KRW" else f"{base}{quote}"
    return SnapshotRow(
        exchange=exchange,
        base=base,
        native_symbol=native,
        quote=quote,
        price=price,
        asks=asks,
        bids=bids,
        deposit_enabled=deposit,
        withdrawal_enabled=withdrawal,
        price_timestamp=ts,
    )


async def seed_rows(session, exchange: str, rows: list[SnapshotRow]) -> None:
    await repository.replace_exchange_snapshots(session, exchange, rows)
    await session.commit()


async def seed_fx_rate(session, rate: float = FX_RATE) -> None:
    """통일 환율(fx_rate 단일 행)을 심는다."""
    await repository.upsert_fx_rate(
        session, rate=rate, source_time=NOW_MS // 1000, round_no=100
    )
    await session.commit()


async def seed_standard(session) -> None:
    """국내 2곳 + 바이낸스 + 환율 — 조회 API 테스트의 표준 시나리오.

    빗썸은 입출금 상태를 알 수 없는 상황(None)으로 심는다.
    """
    await seed_rows(
        session,
        "upbit",
        [snapshot_row("upbit", b, p) for b, p in UPBIT_PRICES.items()],
    )
    await seed_rows(
        session,
        "bithumb",
        [
            snapshot_row("bithumb", b, p, deposit=None, withdrawal=None)
            for b, p in BITHUMB_PRICES.items()
        ],
    )
    await seed_rows(
        session,
        "binance",
        [
            snapshot_row("binance", b, p, quote="USDT", krw_factor=FX_RATE)
            for b, p in BINANCE_PRICES.items()
        ],
    )
    await seed_fx_rate(session)


# ── 기대값 헬퍼 (테스트가 시드와 같은 상수로 직접 계산한다) ─────────────


def best_ask(price: float) -> float:
    return price * (1 + BOOK_STEP)


def best_bid(price: float) -> float:
    return price * (1 - BOOK_STEP)


def fwd_execution_percent(
    domestic_price: float, overseas_price: float, rate: float
) -> float:
    """표면 김프(%) — 국내 최우선 매수호가 / 해외 최우선 매도호가(원화)."""
    return (best_bid(domestic_price) / (best_ask(overseas_price) * rate) - 1) * 100


def rev_execution_percent(
    domestic_price: float, overseas_price: float, rate: float
) -> float:
    """표면 역프(%) — 해외 최우선 매수호가(원화) / 국내 최우선 매도호가."""
    return (best_bid(overseas_price) * rate / best_ask(domestic_price) - 1) * 100


# ── 픽스처 ──────────────────────────────────────────────────────────


@pytest.fixture
async def engine():
    """in-memory SQLite. StaticPool 로 모든 세션이 같은 DB 를 본다."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
def session_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def db(session_factory):
    async with session_factory() as session:
        yield session


@pytest.fixture
async def client(session_factory):
    """실제 앱 + SQLite 세션 주입. lifespan 은 돌리지 않는다."""
    from app.db.database import get_session
    from app.main import app

    async def _override():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.pop(get_session, None)


@pytest.fixture
async def seeded_client(client, db):
    """표준 시나리오가 심어진 클라이언트."""
    await seed_standard(db)
    return client
