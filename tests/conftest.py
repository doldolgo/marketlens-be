"""공용 테스트 픽스처 — in-memory SQLite + FastAPI 세션 주입.

네트워크 호출은 어디에도 없다. 조회 API 는 메모리 아니면 DB 만 읽으므로,
시나리오 데이터를 심어두고 서비스/라우트를 **실제 코드**로 태운다.

- ``engine`` / ``db``     : in-memory SQLite (StaticPool 로 커넥션 하나를 공유)
- ``client``              : httpx.ASGITransport + dependency_overrides[get_session]
                            (lifespan 을 돌리지 않으므로 실제 DB 접속 시도가 없다)
- ``seed_standard``       : 국내 2곳(업비트·빗썸) + 바이낸스 + 통일 환율 표준 시나리오
- ``seed_live_store``     : 같은 시나리오를 **메모리**(live_store)에 심는다

조회 API 는 메모리(live_store)를 먼저 보고, 비어 있으면 DB 로 폴백한다.
``_reset_live_store`` 가 매 테스트 전후로 메모리를 비우므로, 아무것도 하지
않은 테스트는 **DB 폴백 경로**를 검증한다. ``seed_live_store`` 를 함께 쓰면
같은 데이터로 **메모리 경로**를 검증한다.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.db import repository
from app.db.models import Base
from app.db.repository import SnapshotRow
from app.services.live_store import LiveRate, LiveSnapshot, live_store

NOW_MS = 1_700_000_000_000


@pytest.fixture(autouse=True)
def _reset_live_store():
    """메모리 저장소는 프로세스 싱글턴이라 테스트 사이로 새어 나간다.

    수집기 테스트가 채워둔 값이 다음 테스트의 조회 결과를 바꾸지 않도록
    앞뒤로 비운다. 기본 상태가 "비어 있음"이므로 손대지 않은 테스트는
    자연히 DB 폴백 경로를 탄다.
    """
    live_store.clear()
    yield
    live_store.clear()


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
    monkeypatch.setattr(settings, "overseas_quote", "USDT")
    monkeypatch.setattr(settings, "orderbook_max_amount_krw", 1_000_000_000.0)
    monkeypatch.setattr(settings, "default_orderbook_depth", 10)

# ── 표준 시나리오 시세 ──────────────────────────────────────────────
# 환율 1,400 기준 바이낸스 BTC 71,000 USDT = 99,400,000원 → 김프 +0.6% 안팎.
UPBIT_PRICES = {"BTC": 100_000_000.0, "ETH": 5_000_000.0, "XRP": 1_400.0}
BITHUMB_PRICES = {"BTC": 100_100_000.0, "XRP": 1_402.0}
BINANCE_PRICES = {"BTC": 71_000.0, "ETH": 3_550.0, "XRP": 0.99, "SOL": 150.0}
#: 환율 (국내 거래소 KRW-USDT 호가). 기본 시드는 ask == bid == FX_RATE 로 심는다
#: — 방향별 환율을 도입하기 전(단일 환율)과 값이 정확히 같아야 하기 때문이다.
#: 방향 분리를 확인하는 테스트는 ask/bid 를 직접 벌려서 쓴다.
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
    await repository.upsert_exchange_snapshots(session, exchange, rows)
    await session.commit()


async def seed_usdkrw_rate(
    session,
    rate: float = FX_RATE,
    *,
    ask: float | None = None,
    bid: float | None = None,
    exchanges: tuple[str, ...] = ("upbit", "bithumb"),
) -> None:
    """거래소별 KRW-USDT 환율 행을 심는다.

    기본은 ask == bid == rate — 단일 환율 시절과 결과가 같아야 하는 회귀
    시나리오용이다. ask/bid 를 따로 주면 방향별 분리를 확인할 수 있다.
    """
    for exchange in exchanges:
        await repository.upsert_usdkrw_rate(
            session,
            exchange=exchange,
            ask=rate if ask is None else ask,
            bid=rate if bid is None else bid,
        )
    await session.commit()


async def seed_standard(session) -> None:
    """국내 2곳 + 바이낸스 + 환율 — 조회 API 테스트의 표준 시나리오.

    빗썸은 입출금이 **막힌**(False) 상황으로 심는다. "확인 불가"(None)는
    이 시드로 덮지 않는다 — 세 상태를 구분해 태우는 테스트는
    ``test_wallet_state.py`` 가 직접 행을 만들어 쓴다.
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
            snapshot_row("bithumb", b, p, deposit=False, withdrawal=False)
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
    await seed_usdkrw_rate(session)


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


def live_snapshot(
    row: SnapshotRow, *, updated_at: datetime | None = None
) -> LiveSnapshot:
    """DB 시드용 SnapshotRow 를 그대로 메모리 스냅샷으로 옮긴다."""
    return LiveSnapshot(
        exchange=row.exchange,
        base=row.base,
        native_symbol=row.native_symbol,
        quote=row.quote,
        price=row.price,
        asks=row.asks,
        bids=row.bids,
        deposit_enabled=row.deposit_enabled,
        withdrawal_enabled=row.withdrawal_enabled,
        price_timestamp=row.price_timestamp,
        updated_at=updated_at or datetime.now(timezone.utc),
    )


def seed_live_standard(
    rate: float = FX_RATE, *, ask: float | None = None, bid: float | None = None
) -> float:
    """seed_standard 와 **같은 시세**를 메모리에 심는다.

    DB 폴백 경로와 메모리 경로가 같은 결과를 내는지 대조하는 데 쓴다.

    Returns:
        메모리에 심은 시각 (epoch 초) — data_received_at 대조용.
    """
    rows = [
        *(snapshot_row("upbit", b, p) for b, p in UPBIT_PRICES.items()),
        *(
            snapshot_row("bithumb", b, p, deposit=False, withdrawal=False)
            for b, p in BITHUMB_PRICES.items()
        ),
        *(
            snapshot_row("binance", b, p, quote="USDT", krw_factor=rate)
            for b, p in BINANCE_PRICES.items()
        ),
    ]
    received_at = time.time()
    live_store.replace(
        [live_snapshot(r) for r in rows],
        {
            eid: LiveRate(
                exchange=eid,
                ask=rate if ask is None else ask,
                bid=rate if bid is None else bid,
            )
            for eid in ("upbit", "bithumb")
        },
        received_at,
    )
    return received_at


@pytest.fixture
def seed_live_store():
    """표준 시나리오를 메모리에 심는다 (조회가 DB 대신 메모리를 보게 된다)."""
    return seed_live_standard()


@pytest.fixture
async def seeded_client(client, db):
    """표준 시나리오가 심어진 클라이언트."""
    await seed_standard(db)
    return client


# ── 수집 사이클 하네스 (거래소 호출을 전부 대체한다) ──────────────────


async def refresh_once(
    service,
    db,
    monkeypatch,
    *,
    domestic_bases,
    binance_bases,
    wallet_status=None,
    wallet_fails=False,
):
    """거래소 호출을 전부 대체해 수집 사이클 한 번을 돌린다.

    ``wallet_status`` 로 지갑 응답을 코인별로 지정할 수 있다 (기본: 전부 열림).
    ``wallet_fails=True`` 면 지갑 조회가 실패한 상황을 만든다 — 수집기는 이때
    입출금을 **확인 불가(None)** 로 둬야 한다.
    """
    from app.exchanges.private.wallet_status import WalletStatus
    from app.models.bulk import BulkQuote
    from app.models.orderbook import MarketType, OrderBook, OrderBookLevel

    def book(base: str, exchange: str) -> OrderBook:
        return OrderBook(
            exchange=exchange,
            symbol=f"{base}/KRW",
            native_symbol=f"KRW-{base}",
            market_type=MarketType.SPOT,
            base=base,
            quote="KRW",
            bids=[OrderBookLevel(price=99.0, size=1.0)],
            asks=[OrderBookLevel(price=101.0, size=1.0)],
            timestamp=1_700_000_000_000,
            latency_ms=1.0,
        )

    def usdt_book(exchange: str) -> OrderBook:
        """KRW-USDT 호가 — 수집기가 여기서 환율을 뽑는다 (추가 호출 없음)."""
        return OrderBook(
            exchange=exchange,
            symbol="USDT/KRW",
            native_symbol="KRW-USDT",
            market_type=MarketType.SPOT,
            base="USDT",
            quote="KRW",
            bids=[OrderBookLevel(price=FX_RATE, size=1000.0)],
            asks=[OrderBookLevel(price=FX_RATE, size=1000.0)],
            timestamp=1_700_000_000_000,
            latency_ms=1.0,
        )

    async def domestic(eid, failures):
        books = {b: book(b, eid) for b in domestic_bases}
        books["USDT"] = usdt_book(eid)
        lasts = dict.fromkeys(domestic_bases, 140_000.0)
        lasts["USDT"] = FX_RATE
        return eid, books, lasts

    async def binance(bases, failures):
        tops = {
            b: BulkQuote(
                base=b,
                quote="USDT",
                native_symbol=f"{b}USDT",
                bid=99.9,
                bid_size=1.0,
                ask=100.0,
                ask_size=1.0,
            )
            for b in binance_bases
        }
        return "binance", tops, dict.fromkeys(binance_bases, 100.0)

    async def futures(warnings):
        return 1

    async def wallet(eid, warnings):
        if wallet_fails:
            # _wallet() 은 예외를 삼키고 None 을 돌려준다 (경고만 남긴다)
            warnings.append(f"{eid} 입출금 상태 조회 실패 — 테스트")
            return None
        if wallet_status is not None:
            return dict(wallet_status)
        return {b: WalletStatus(deposit=True, withdrawal=True) for b in domestic_bases}

    monkeypatch.setattr(service, "_domestic_market", domestic)
    monkeypatch.setattr(service, "_binance_market", binance)
    monkeypatch.setattr(service, "_binance_futures_count", futures)
    monkeypatch.setattr(service, "_wallet", wallet)
    await service.refresh(db)
