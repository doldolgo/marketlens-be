"""전종목 스캔 로직 테스트 — DB 스냅샷 기반 (네트워크 불필요)."""

from __future__ import annotations

import pytest
from conftest import (
    NOW_MS,
    seed_usdkrw_rate,
    seed_rows,
    seed_standard,
    snapshot_row,
)

from app.core.config import settings
from app.core.errors import (
    MarketDataNotFoundError,
    UnsupportedExchangeError,
)
from app.db.models import MarketSnapshot
from app.models.scan import SortOrder
from app.models.ticker import PriceSide
from app.services.scan_service import ScanService, _pick, scan_service


def make_snap(
    *,
    price: float = 100.0,
    bids: list | None = None,
    asks: list | None = None,
) -> MarketSnapshot:
    return MarketSnapshot(
        exchange="upbit",
        base="BTC",
        native_symbol="KRW-BTC",
        quote="KRW",
        price=price,
        bids=bids if bids is not None else [],
        asks=asks if asks is not None else [],
        price_timestamp=NOW_MS,
    )


class TestPick:
    """스냅샷에서 (가격, 최우선 호가 잔량) 을 뽑는다."""

    def snap(self) -> MarketSnapshot:
        return make_snap(bids=[[90.0, 2.0], [89.0, 1.0]], asks=[[110.0, 3.0]])

    def test_bid_and_ask_carry_top_level_size(self) -> None:
        assert _pick(self.snap(), PriceSide.BID) == (90.0, 2.0)
        assert _pick(self.snap(), PriceSide.ASK) == (110.0, 3.0)

    def test_missing_book_returns_none(self) -> None:
        empty = make_snap(price=100.0)
        assert _pick(empty, PriceSide.BID) is None
        assert _pick(empty, PriceSide.ASK) is None


class TestScan:
    async def test_empty_db_raises(self, db) -> None:
        with pytest.raises(MarketDataNotFoundError):
            await scan_service.scan(db)

    async def test_missing_domestic_snapshots_raises(self, db) -> None:
        await seed_rows(
            db,
            "binance",
            [snapshot_row("binance", "BTC", 71_000.0, quote="USDT", krw_factor=1400)],
        )
        await seed_usdkrw_rate(db)
        with pytest.raises(MarketDataNotFoundError):
            await scan_service.scan(db)

    async def test_missing_overseas_snapshots_raises(self, db) -> None:
        await seed_rows(db, "upbit", [snapshot_row("upbit", "BTC", 100_000_000.0)])
        await seed_usdkrw_rate(db)
        with pytest.raises(MarketDataNotFoundError):
            await scan_service.scan(db)

    async def test_finds_best_of_each_direction(self, db) -> None:
        await seed_standard(db)
        res = await scan_service.scan(db)

        # 국내(업비트) ∩ 해외(바이낸스) = BTC, ETH, XRP. SOL 은 국내 미상장.
        assert res.scanned_coins == 3
        assert res.scanned_pairs == 3
        assert res.dom == "upbit"
        assert res.fx_list == ["binance"]

        assert res.best_fwd is not None
        # 시드에서 XRP 김프가 가장 크다 (bid 1399.3 / ask 0.9905×1400)
        assert res.best_fwd.sym == "XRP"
        assert res.best_rev is not None
        # 역프는 전부 음수인 시나리오 — best 라도 손해일 수 있다
        assert res.best_rev.premium_percent < 0

    async def test_default_asc_order(self, db) -> None:
        await seed_standard(db)
        res = await scan_service.scan(db)

        assert res.order is SortOrder.ASC
        percents = [e.premium_percent for e in res.top_fwd]
        assert percents == sorted(percents)
        # best 는 정렬과 무관하게 항상 최대값
        assert res.best_fwd.premium_percent >= max(percents)

    async def test_desc_order_reverses_lists(self, db) -> None:
        await seed_standard(db)
        res = await scan_service.scan(db, order=SortOrder.DESC)

        percents = [e.premium_percent for e in res.top_fwd]
        assert percents == sorted(percents, reverse=True)
        assert res.best_fwd.premium_percent == percents[0]

    async def test_domestic_selection_changes_universe(self, db) -> None:
        await seed_standard(db)
        res = await scan_service.scan(db, domestic="bithumb")

        # 빗썸에는 BTC, XRP 만 있다
        assert res.dom == "bithumb"
        assert res.scanned_coins == 2

    async def test_min_liquidity_filters_thin_books(self, db) -> None:
        """시드의 최우선 호가는 단계당 300만원어치 — 그보다 큰 필터는 전부 걸러낸다."""
        await seed_standard(db)
        res = await scan_service.scan(db, min_liquidity_krw=100_000_000.0)

        assert res.filtered_out > 0
        assert res.best_fwd is None
        assert res.top_fwd == []

    async def test_suspicious_premium_is_flagged(self, db) -> None:
        """티커 충돌 수준(±5% 이상)의 프리미엄은 의심으로 표시된다."""
        await seed_rows(db, "upbit", [snapshot_row("upbit", "AI", 168_000.0)])
        await seed_rows(
            db,
            "binance",
            [snapshot_row("binance", "AI", 100.0, quote="USDT", krw_factor=1400)],
        )
        await seed_usdkrw_rate(db)

        res = await scan_service.scan(db)
        assert res.best_fwd.sym == "AI"
        assert res.best_fwd.premium_percent > settings.scan_suspicious_percent
        assert res.best_fwd.suspicious is True
        assert res.best_fwd.suspicion_reason is not None
        assert res.suspicious_count > 0
        assert any("의심" in w for w in res.warnings)

    async def test_unknown_overseas_exchange_raises(self, db) -> None:
        await seed_standard(db)
        with pytest.raises(UnsupportedExchangeError):
            await scan_service.scan(db, exchanges=["coinbase"])

    async def test_premium_formula_matches_premium_service(self, db) -> None:
        """스캔의 수익률은 /premium 과 완전히 동일한 공식이어야 한다."""
        from app.models.premium import PremiumDirection
        from app.services.premium_service import premium_service

        await seed_standard(db)
        scan_res = await scan_service.scan(db)
        btc = next(e for e in scan_res.top_fwd if e.sym == "BTC")

        premium_res = await premium_service.fetch_premiums(
            db,
            "BTC",
            direction=PremiumDirection.FWD,
        )
        assert btc.premium_percent == pytest.approx(
            premium_res.premiums[0].premium_percent
        )


class TestSortOrder:
    def test_values_are_url_friendly(self) -> None:
        assert SortOrder.ASC.value == "asc"
        assert SortOrder.DESC.value == "desc"

    def test_default_is_ascending(self) -> None:
        import inspect

        sig = inspect.signature(ScanService.scan)
        assert sig.parameters["order"].default is SortOrder.ASC
