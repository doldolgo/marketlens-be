"""DB 저장 루프 — 수집 사이클에서 분리된 별도 주기.

지켜야 할 계약은 하나다: **수집은 DB 를 건드리지 않고, 저장만 건드린다.**
수집이 1초마다 도는데 DB 쓰기가 딸려 오면 사이클의 85% 를 다시 쓰기에
쓰게 된다 (전환 전 2.6초 중 2.2초).
"""

from __future__ import annotations

from sqlalchemy import func, select

from app.core.config import settings
from app.db.models import MarketSnapshot, PlatformStatus, UsdKrwRate
from app.services import collector_service as collector_module
from app.services.collector_service import CollectorService
from app.services.live_store import live_store
from conftest import refresh_once


async def _snapshot_count(db) -> int:
    return (
        await db.execute(select(func.count()).select_from(MarketSnapshot))
    ).scalar_one()


async def _updated_ats(db) -> list:
    result = await db.execute(select(MarketSnapshot.updated_at))
    return sorted(result.scalars())


# ── 수집은 DB 를 건드리지 않는다 ────────────────────────────────────


async def test_collect_writes_nothing_to_db(db, monkeypatch):
    """수집 사이클을 여러 번 돌려도 market_snapshots 는 비어 있다."""
    service = CollectorService()
    for _ in range(3):
        await refresh_once(
            service, db, monkeypatch, domestic_bases=["BTC"], binance_bases=["BTC"]
        )

    assert not live_store.is_empty(), "메모리에는 들어가야 한다"
    assert await _snapshot_count(db) == 0, "수집이 DB 를 건드렸다"


async def test_persist_writes_what_memory_holds(db, monkeypatch):
    """저장 루프가 돌면 그제서야 DB 에 내려간다."""
    service = CollectorService()
    await refresh_once(
        service, db, monkeypatch, domestic_bases=["BTC", "ETH"],
        binance_bases=["BTC", "ETH"],
    )
    result = await service.persist(db)

    # 국내 2곳 + 바이낸스 = 코인당 3행
    assert result.saved == 6
    assert await _snapshot_count(db) == 6

    # 플랫폼 상태와 환율도 저장 루프가 함께 내린다.
    statuses = (await db.execute(select(PlatformStatus))).scalars().all()
    assert {s.exchange for s in statuses} == {"upbit", "bithumb", "binance"}
    assert (await db.get(UsdKrwRate, 1)).rate == 1400.0


async def test_many_collects_produce_one_db_write(db, monkeypatch):
    """수집이 여러 번 돌아도 DB 갱신은 저장 주기당 한 번뿐이다.

    updated_at 이 몇 번 움직이는지로 확인한다 — 수집마다 썼다면 사이클 수만큼
    달라졌을 것이다.
    """
    service = CollectorService()
    for _ in range(5):
        await refresh_once(
            service, db, monkeypatch, domestic_bases=["BTC"], binance_bases=["BTC"]
        )
    await service.persist(db)
    after_first = await _updated_ats(db)

    for _ in range(5):
        await refresh_once(
            service, db, monkeypatch, domestic_bases=["BTC"], binance_bases=["BTC"]
        )

    assert await _updated_ats(db) == after_first, "저장 루프 밖에서 DB 가 갱신됐다"


async def test_persist_before_any_collect_is_a_noop(db):
    """기동 직후 — 메모리가 비어 있으면 쓸 것이 없다."""
    service = CollectorService()
    result = await service.persist(db)

    assert result.saved == 0
    assert await _snapshot_count(db) == 0


async def test_persist_removes_coins_that_left_memory(db, monkeypatch):
    """짝을 잃어 메모리에서 빠진 코인은 다음 저장 때 DB 에서도 지워진다."""
    service = CollectorService()
    await refresh_once(
        service, db, monkeypatch, domestic_bases=["BTC", "DOGE"],
        binance_bases=["BTC", "DOGE"],
    )
    await service.persist(db)
    assert await _snapshot_count(db) == 6

    # DOGE 가 바이낸스에서 빠졌다 = 김프를 계산할 수 없다 = 메모리에서 제외된다.
    await refresh_once(
        service, db, monkeypatch, domestic_bases=["BTC", "DOGE"],
        binance_bases=["BTC"],
    )
    result = await service.persist(db)

    assert result.deleted == 3, "DOGE 3행이 지워져야 한다"
    bases = (await db.execute(select(MarketSnapshot.base).distinct())).scalars().all()
    assert set(bases) == {"BTC"}


async def test_archive_throttles_independently_of_persist(db, monkeypatch):
    """아카이브 주기가 저장 주기보다 길면 저장이 돌아도 아카이브는 안 쌓인다.

    손잡이를 따로 둔 이유가 이것이다 — 스냅샷은 행 수가 고정된 미러지만
    premium_archive 는 append 전용이라 주기가 곧 DB 증가 속도다.
    """
    monkeypatch.setattr(settings, "archive_interval_seconds", 3600.0)
    service = CollectorService()
    await refresh_once(
        service, db, monkeypatch, domestic_bases=["BTC"], binance_bases=["BTC"]
    )

    first = await service.persist(db)
    assert first.archived > 0, "첫 회차는 남겨야 한다"

    await refresh_once(
        service, db, monkeypatch, domestic_bases=["BTC"], binance_bases=["BTC"]
    )
    second = await service.persist(db)

    assert second.saved > 0, "스냅샷은 매 저장마다 내려야 한다"
    assert second.archived == 0, "아카이브 주기가 안 지났는데 쌓였다"


async def test_first_archive_does_not_depend_on_machine_uptime(db, monkeypatch):
    """갓 부팅한 기계에서도 첫 회차는 적재해야 한다.

    ``time.monotonic()`` 은 부팅 후 경과 시간이다. 마지막 실행 시각을 0.0 으로
    두면 "아주 옛날"이라는 뜻이 되지 못한다 — uptime 이 주기보다 짧으면
    ``now - 0 >= interval`` 이 거짓이라 첫 적재가 통째로 사라진다.
    개발자 기계는 며칠씩 켜져 있어 안 드러나고, 방금 뜬 CI 러너에서만 터졌다.
    """
    monkeypatch.setattr(settings, "archive_interval_seconds", 3600.0)
    monkeypatch.setattr(collector_module.time, "monotonic", lambda: 5.0)  # uptime 5초

    service = CollectorService()
    await refresh_once(
        service, db, monkeypatch, domestic_bases=["BTC"], binance_bases=["BTC"]
    )
    assert (await service.persist(db)).archived > 0
