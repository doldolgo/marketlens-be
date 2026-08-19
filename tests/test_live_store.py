"""메모리 저장소(live_store)와 콜드스타트 DB 폴백.

이 파일이 지키는 것은 두 가지다.

1. ``LiveStore`` 자체의 계약 — 통째 교체, 없을 때의 예외
2. **메모리 경로와 DB 폴백 경로가 같은 답을 낸다** — 조회 API 의 데이터
   출처를 바꾼 이번 작업의 핵심 안전장치다. 둘이 갈리면 재기동 직후와
   평상시가 다른 값을 보여주게 된다.
"""

from __future__ import annotations

import time

import pytest

from app.core.errors import MarketDataNotFoundError
from app.services.live_store import LiveRate, LiveSnapshot, LiveStore, live_store
from conftest import refresh_once, seed_live_standard, seed_standard


def _snap(exchange: str, base: str, price: float = 100.0) -> LiveSnapshot:
    return LiveSnapshot(
        exchange=exchange,
        base=base,
        native_symbol=f"KRW-{base}",
        quote="KRW",
        price=price,
        asks=[[price * 1.001, 1.0]],
        bids=[[price * 0.999, 1.0]],
    )


# ── LiveStore 계약 ──────────────────────────────────────────────────


def test_replace_swaps_the_whole_set():
    """이전 사이클에만 있던 코인은 사라진다 (상폐 코인 자동 제거)."""
    store = LiveStore()
    store.replace([_snap("upbit", "BTC"), _snap("upbit", "DOGE")], None, time.time())
    assert {s.base for s in store.get_snapshots()} == {"BTC", "DOGE"}

    # 다음 사이클에 DOGE 가 빠졌다 = 상장폐지.
    store.replace([_snap("upbit", "BTC")], None, time.time())
    assert {s.base for s in store.get_snapshots()} == {"BTC"}
    assert store.get_snapshot("upbit", "DOGE") is None


def test_replace_keeps_previous_rate_when_none():
    """환율 수집만 실패한 사이클은 직전 환율을 유지한다."""
    store = LiveStore()
    store.replace([_snap("upbit", "BTC")], LiveRate(rate=1400.0), time.time())
    store.replace([_snap("upbit", "BTC")], None, time.time())
    assert store.require_usdkrw_rate().rate == 1400.0


def test_get_snapshots_filters_like_repository():
    """exchange / base 필터가 repository.get_snapshots 와 같게 동작한다."""
    store = LiveStore()
    store.replace(
        [_snap("upbit", "BTC"), _snap("bithumb", "BTC"), _snap("upbit", "ETH")],
        None,
        time.time(),
    )
    assert len(store.get_snapshots(exchange="upbit")) == 2
    assert len(store.get_snapshots(base="btc")) == 2  # 소문자도 받는다
    assert len(store.get_snapshots(exchange="upbit", base="ETH")) == 1


def test_require_snapshot_raises_when_missing():
    store = LiveStore()
    store.replace([_snap("upbit", "BTC")], None, time.time())
    with pytest.raises(MarketDataNotFoundError):
        store.require_snapshot("upbit", "DOGE")


def test_require_usdkrw_rate_raises_when_missing_or_zero():
    store = LiveStore()
    with pytest.raises(MarketDataNotFoundError):
        store.require_usdkrw_rate()

    store.replace([], LiveRate(rate=0.0), time.time())
    with pytest.raises(MarketDataNotFoundError):
        store.require_usdkrw_rate()


def test_is_empty_and_received_at():
    store = LiveStore()
    assert store.is_empty()
    assert store.received_at is None

    at = time.time()
    store.replace([_snap("upbit", "BTC")], LiveRate(rate=1400.0), at)
    assert not store.is_empty()
    assert store.received_at == at


def test_updated_at_is_timezone_aware():
    """naive 로 새면 spread_service._age_seconds() 의 보정 분기를 타게 된다."""
    snap = _snap("upbit", "BTC")
    assert snap.updated_at.tzinfo is not None
    assert LiveRate(rate=1400.0).updated_at.tzinfo is not None


# ── 콜드스타트 폴백 ─────────────────────────────────────────────────


async def test_cold_start_serves_db_instead_of_404(client, db):
    """재기동 직후 첫 사이클 전 — 메모리가 비어도 404 가 아니라 DB 값."""
    await seed_standard(db)
    assert live_store.is_empty()

    for path in ("/spreads", "/matrix", "/arbitrage?sym=BTC&amount=1000000"):
        res = await client.get(path)
        assert res.status_code == 200, (path, res.text)


async def test_memory_path_is_used_once_filled(client, db):
    """메모리가 차 있으면 DB 가 비어 있어도 조회가 된다 (= 진짜 메모리를 읽는다)."""
    seed_live_standard()  # DB 는 비어 있는 상태
    res = await client.get("/spreads")
    assert res.status_code == 200
    assert res.json()["rows"], "메모리에서 읽지 못했다"


@pytest.mark.parametrize(
    "path",
    [
        "/spreads",
        "/matrix",
        "/arbitrage?sym=BTC&amount=1000000",
        "/slippage/upbit?symbol=BTC/KRW&amount=1000000",
        "/premium/fwd?sym=BTC",
        "/premium?sym=BTC",
        "/compare?sym=BTC",
    ],
)
async def test_all_query_endpoints_equal_on_both_paths(client, db, path):
    """같은 데이터를 DB 와 메모리에 각각 넣으면 같은 응답이 나와야 한다.

    시각·소요시간처럼 매 호출 달라지는 필드만 빼고 통째로 대조한다.
    """
    await seed_standard(db)
    from_db = await client.get(path)
    assert from_db.status_code == 200, from_db.text

    seed_live_standard()
    from_memory = await client.get(path)
    assert from_memory.status_code == 200, from_memory.text

    assert _stable(from_memory.json()) == _stable(from_db.json())


#: 호출마다 달라지는 필드 — 등가 비교에서 제외한다.
#
# ``data_*_at`` 계열은 스냅샷을 **언제 심었는지**라 두 경로가 같을 수 없다
# (DB 시드가 먼저, 메모리 시드가 나중). 비교 대상은 계산 결과지 시각이 아니다.
_VOLATILE = {
    "fetched_at",
    "elapsed_ms",
    "age",
    "data_updated_at",
    "data_oldest_at",
    "data_newest_at",
    "rate_updated_at",
    "oldest",
    "newest",
}


def _stable(value):
    """응답에서 시각·소요시간 계열 필드를 재귀적으로 걷어낸다."""
    if isinstance(value, dict):
        return {k: _stable(v) for k, v in value.items() if k not in _VOLATILE}
    if isinstance(value, list):
        return [_stable(v) for v in value]
    return value


# ── 수집기 → 메모리 적재 ────────────────────────────────────────────


async def test_refresh_fills_memory_with_the_intersection(db, monkeypatch):
    """수집 사이클이 메모리를 채운다 — 담기는 건 국내 ∩ 해외 교집합이다."""
    from app.services.collector_service import CollectorService

    service = CollectorService()
    await refresh_once(
        service,
        db,
        monkeypatch,
        domestic_bases=["BTC", "ONLYKRW"],
        binance_bases=["BTC", "ONLYUSDT"],
    )

    assert not live_store.is_empty()
    assert live_store.received_at is not None
    # 한쪽 시장에만 있는 코인은 김프를 계산할 수 없어 담지 않는다.
    assert {s.base for s in live_store.get_snapshots()} == {"BTC"}
    assert live_store.require_usdkrw_rate().rate == 1400.0


async def test_refresh_drops_delisted_coins_from_memory(db, monkeypatch):
    """다음 사이클에서 빠진 코인은 메모리에서도 사라진다 (통째 교체)."""
    from app.services.collector_service import CollectorService

    service = CollectorService()
    await refresh_once(
        service, db, monkeypatch, domestic_bases=["BTC", "DOGE"],
        binance_bases=["BTC", "DOGE"],
    )
    assert {s.base for s in live_store.get_snapshots()} == {"BTC", "DOGE"}

    await refresh_once(
        service, db, monkeypatch, domestic_bases=["BTC"], binance_bases=["BTC"]
    )
    assert {s.base for s in live_store.get_snapshots()} == {"BTC"}
