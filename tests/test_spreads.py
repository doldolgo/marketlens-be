"""스프레드 테이블 테스트 — DB 스냅샷 기반 (네트워크 불필요)."""

from __future__ import annotations

import pytest
from conftest import (
    BINANCE_PRICES,
    BITHUMB_PRICES,
    KRW_RATES,
    LEVEL_AMOUNT_KRW,
    UPBIT_PRICES,
    fwd_execution_percent,
    rev_execution_percent,
    seed_rates,
    seed_rows,
    seed_standard,
    snapshot_row,
)

from app.core.errors import MarketDataNotFoundError
from app.db.repository import SnapshotRow
from app.models.spread import FeedStatus
from app.services.spread_service import spread_service

BOOK_STEP = 0.0005  # conftest 표준 호가의 1단계 스프레드


class TestBuild:
    async def test_empty_db_raises(self, db) -> None:
        with pytest.raises(MarketDataNotFoundError):
            await spread_service.build(db)

    async def test_missing_rates_raise(self, db) -> None:
        await seed_rows(db, "upbit", [snapshot_row("upbit", "BTC", 100_000_000.0)])
        with pytest.raises(MarketDataNotFoundError):
            await spread_service.build(db)

    async def test_all_pairs_are_returned(self, db) -> None:
        """페어 = 국내 × 해외 × 양쪽 상장 코인. SOL 은 국내 미상장이라 빠진다."""
        await seed_standard(db)
        res = await spread_service.build(db)

        keys = {(r.sym, r.dom, r.fx) for r in res.rows}
        assert keys == {
            ("BTC", "upbit", "binance"),
            ("ETH", "upbit", "binance"),
            ("XRP", "upbit", "binance"),
            ("BTC", "bithumb", "binance"),
            ("XRP", "bithumb", "binance"),
        }
        # sym → dom → fx 정렬
        assert [(r.sym, r.dom) for r in res.rows] == sorted(
            (r.sym, r.dom) for r in res.rows
        )
        assert res.rate == KRW_RATES["upbit"]  # 기준 거래소 환율

    async def test_fwd_and_rev_match_premium_formula(self, db) -> None:
        """한 행의 fwd/rev 는 /premium/fwd · /premium/rev 와 같은 공식이다."""
        await seed_standard(db)
        res = await spread_service.build(db)

        btc = next(r for r in res.rows if r.sym == "BTC" and r.dom == "upbit")
        assert btc.fwd == pytest.approx(
            fwd_execution_percent(UPBIT_PRICES["BTC"], BINANCE_PRICES["BTC"], 1400.0)
        )
        assert btc.rev == pytest.approx(
            rev_execution_percent(UPBIT_PRICES["BTC"], BINANCE_PRICES["BTC"], 1400.0)
        )
        assert btc.usd == BINANCE_PRICES["BTC"]  # 해외 마지막 체결가

    async def test_each_domestic_uses_own_rate(self, db) -> None:
        """빗썸 페어는 빗썸 자기 환율(1401)로 계산된다."""
        await seed_standard(db)
        res = await spread_service.build(db)

        bithumb = next(r for r in res.rows if r.sym == "BTC" and r.dom == "bithumb")
        assert bithumb.fwd == pytest.approx(
            fwd_execution_percent(
                BITHUMB_PRICES["BTC"], BINANCE_PRICES["BTC"], KRW_RATES["bithumb"]
            )
        )

    async def test_liquidity_is_split_per_venue_in_usd(self, db) -> None:
        """liqDom/liqFx 는 양측을 나눠 담고, 둘 다 USD(T) 기준이다.

        표준 시드는 한 단계가 원화 300만원어치 — 최우선 매수/매도 중 작은 쪽은
        bid 쪽(가격 × (1-스프레드))이다.
        """
        await seed_standard(db)
        res = await spread_service.build(db)

        btc = next(r for r in res.rows if r.sym == "BTC" and r.dom == "upbit")
        expected = LEVEL_AMOUNT_KRW * (1 - BOOK_STEP)
        assert btc.liq_dom == pytest.approx(expected / 1400.0)
        assert btc.liq_fx == pytest.approx(expected / 1400.0)

    async def test_fresh_data_is_ok_and_spark_is_empty(self, db) -> None:
        await seed_standard(db)
        res = await spread_service.build(db)

        for r in res.rows:
            assert r.status is FeedStatus.OK
            assert r.age < 30
            assert r.spark == []

    async def test_empty_book_is_fail_with_zeros(self, db) -> None:
        await seed_rows(
            db,
            "upbit",
            [
                SnapshotRow(
                    exchange="upbit",
                    base="BTC",
                    native_symbol="KRW-BTC",
                    quote="KRW",
                    price=100_000_000.0,
                    asks=[],
                    bids=[],
                )
            ],
        )
        await seed_rows(
            db,
            "binance",
            [snapshot_row("binance", "BTC", 71_000.0, quote="USDT", krw_factor=1400)],
        )
        await seed_rates(db, {"upbit": 1400.0})

        res = await spread_service.build(db)
        row = res.rows[0]
        assert row.status is FeedStatus.FAIL
        assert row.fwd == 0.0 and row.rev == 0.0 and row.usd == 0.0
        assert row.liq_dom == 0.0 and row.liq_fx == 0.0

    async def test_excluded_bases_are_skipped(self, db, monkeypatch) -> None:
        from app.core.config import settings

        monkeypatch.setattr(settings, "scan_excluded_bases", ["BTC"])
        await seed_standard(db)
        res = await spread_service.build(db)
        assert all(r.sym != "BTC" for r in res.rows)


class TestEndpoint:
    async def test_returns_fe_shaped_rows(self, seeded_client) -> None:
        """응답 키가 FE SpreadRow 계약(camelCase 포함)과 일치해야 한다."""
        d = (await seeded_client.get("/spreads")).json()

        assert d["rate"] == KRW_RATES["upbit"]
        row = d["rows"][0]
        assert set(row) == {
            "sym", "dom", "fx", "fwd", "rev", "usd",
            "spark", "status", "age", "liqDom", "liqFx",
        }
        assert row["status"] in ("ok", "stale", "fail")
        assert row["spark"] == []

    async def test_empty_db_is_404(self, client) -> None:
        assert (await client.get("/spreads")).status_code == 404
