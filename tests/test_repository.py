"""DB repository 테스트 — in-memory SQLite 로 UPSERT/삭제/조회를 검증한다."""

from __future__ import annotations

import pytest
from conftest import NOW_MS, seed_usdkrw_rate, snapshot_row

from app.core.errors import MarketDataNotFoundError
from app.db import repository
from app.db.models import MarketSnapshot
from app.models.orderbook import MarketType


class TestUpsertExchangeSnapshots:
    async def test_inserts_rows(self, db) -> None:
        saved = await repository.upsert_exchange_snapshots(
            db,
            "upbit",
            [
                snapshot_row("upbit", "BTC", 100_000_000.0),
                snapshot_row("upbit", "ETH", 5_000_000.0),
            ],
        )
        await db.commit()

        assert saved == 2
        snaps = await repository.get_snapshots(db, exchange="upbit")
        assert {s.base for s in snaps} == {"BTC", "ETH"}

    async def test_updates_in_place_and_keeps_missing(self, db) -> None:
        """코인을 찾아 갱신만 한다 — 이번 수집에 없는 코인도 지우지 않는다."""
        await repository.upsert_exchange_snapshots(
            db,
            "upbit",
            [
                snapshot_row("upbit", "BTC", 100_000_000.0),
                snapshot_row("upbit", "DOGE", 300.0),
            ],
        )
        await db.commit()

        saved = await repository.upsert_exchange_snapshots(
            db,
            "upbit",
            [snapshot_row("upbit", "BTC", 101_000_000.0, deposit=False)],
        )
        await db.commit()

        assert saved == 1
        snaps = {
            s.base: s for s in await repository.get_snapshots(db, exchange="upbit")
        }
        assert set(snaps) == {"BTC", "DOGE"}  # DOGE 는 남아 있다 (삭제 없음)
        assert snaps["BTC"].price == 101_000_000.0  # UPSERT 로 가격이 갱신됐다
        assert snaps["BTC"].deposit_enabled is False  # 입출금 플래그도 갱신됐다

    async def test_empty_rows_touch_nothing(self, db) -> None:
        """빈 수집 결과는 아무것도 저장하지도, 지우지도 않는다."""
        await repository.upsert_exchange_snapshots(
            db, "upbit", [snapshot_row("upbit", "BTC", 100_000_000.0)]
        )
        await db.commit()

        saved = await repository.upsert_exchange_snapshots(db, "upbit", [])
        await db.commit()

        assert saved == 0
        assert len(await repository.get_snapshots(db, exchange="upbit")) == 1


class TestUsdKrwRate:
    async def test_upsert_keeps_one_row_per_exchange(self, db) -> None:
        """환율은 거래소당 한 행 — 같은 거래소를 갱신해도 행이 늘지 않는다."""
        await repository.upsert_usdkrw_rate(db, exchange="upbit", ask=1401.0, bid=1400.0)
        await repository.upsert_usdkrw_rate(db, exchange="bithumb", ask=1405.0, bid=1398.0)
        await db.commit()
        await repository.upsert_usdkrw_rate(db, exchange="upbit", ask=1411.0, bid=1410.0)
        await db.commit()

        rows = await repository.get_usdkrw_rates(db)
        assert set(rows) == {"upbit", "bithumb"}
        assert (rows["upbit"].ask, rows["upbit"].bid) == (1411.0, 1410.0)
        # 거래소마다 테더 프리미엄이 달라 값도 따로 유지돼야 한다.
        assert (rows["bithumb"].ask, rows["bithumb"].bid) == (1405.0, 1398.0)

    async def test_require_usdkrw_rate_raises_when_missing(self, db) -> None:
        with pytest.raises(MarketDataNotFoundError):
            await repository.require_usdkrw_rate(db, "upbit")

    async def test_require_usdkrw_rate_is_per_exchange(self, db) -> None:
        """한 거래소만 심어도 다른 거래소는 여전히 없는 것이다."""
        await repository.upsert_usdkrw_rate(db, exchange="upbit", ask=1401.0, bid=1400.0)
        await db.commit()
        assert (await repository.require_usdkrw_rate(db, "upbit")).ask == 1401.0
        with pytest.raises(MarketDataNotFoundError):
            await repository.require_usdkrw_rate(db, "bithumb")

    async def test_require_usdkrw_rate_rejects_non_positive(self, db) -> None:
        """0 이하 환율은 없는 것으로 취급한다 — 나눗셈 보호."""
        await repository.upsert_usdkrw_rate(db, exchange="upbit", ask=0.0, bid=0.0)
        await db.commit()
        with pytest.raises(MarketDataNotFoundError):
            await repository.require_usdkrw_rate(db, "upbit")


class TestQueries:
    async def _seed(self, db) -> None:
        await repository.upsert_exchange_snapshots(
            db,
            "upbit",
            [
                snapshot_row("upbit", "BTC", 100_000_000.0),
                snapshot_row("upbit", "ETH", 5_000_000.0),
            ],
        )
        await repository.upsert_exchange_snapshots(
            db,
            "binance",
            [snapshot_row("binance", "BTC", 71_000.0, quote="USDT")],
        )
        await db.commit()

    async def test_filter_by_exchange_and_base(self, db) -> None:
        await self._seed(db)

        assert len(await repository.get_snapshots(db)) == 3
        assert len(await repository.get_snapshots(db, exchange="upbit")) == 2
        assert len(await repository.get_snapshots(db, base="BTC")) == 2
        only = await repository.get_snapshots(db, exchange="binance", base="BTC")
        assert [s.exchange for s in only] == ["binance"]

    async def test_base_lookup_is_case_insensitive(self, db) -> None:
        await self._seed(db)

        snap = await repository.get_snapshot(db, "upbit", "btc")
        assert snap is not None and snap.base == "BTC"
        assert len(await repository.get_snapshots(db, base="btc")) == 2

    async def test_require_snapshot_raises_when_missing(self, db) -> None:
        with pytest.raises(MarketDataNotFoundError) as exc_info:
            await repository.require_snapshot(db, "upbit", "btc")
        assert exc_info.value.detail["base"] == "BTC"

    async def test_updated_at_is_set_by_db(self, db) -> None:
        await self._seed(db)
        snap = await repository.get_snapshot(db, "upbit", "BTC")
        assert snap.updated_at is not None


class TestConversionHelpers:
    def test_levels_from_json(self) -> None:
        out = repository.levels_from_json([[100.0, 1.5], ["101", "2"]])

        assert [(lv.price, lv.size) for lv in out] == [(100.0, 1.5), (101.0, 2.0)]

    def test_orderbook_from_snapshot(self) -> None:
        snap = MarketSnapshot(
            exchange="upbit",
            base="BTC",
            native_symbol="KRW-BTC",
            quote="KRW",
            price=100.0,
            asks=[[101.0, 1.0], [102.0, 1.0], [103.0, 1.0]],
            bids=[[99.0, 1.0], [98.0, 1.0], [97.0, 1.0]],
            price_timestamp=NOW_MS,
        )
        book = repository.orderbook_from_snapshot(snap, depth=2)

        assert book.exchange == "upbit"
        assert book.symbol == "BTC/KRW"
        assert book.native_symbol == "KRW-BTC"
        assert book.market_type is MarketType.SPOT
        assert book.timestamp == NOW_MS
        assert len(book.asks) == 2 and len(book.bids) == 2  # depth 로 잘린다
        assert book.best_ask == 101.0 and book.best_bid == 99.0

    def test_orderbook_from_snapshot_without_depth_keeps_all(self) -> None:
        snap = MarketSnapshot(
            exchange="upbit",
            base="BTC",
            native_symbol="KRW-BTC",
            quote="KRW",
            price=100.0,
            asks=[[101.0, 1.0], [102.0, 1.0]],
            bids=[[99.0, 1.0]],
            price_timestamp=NOW_MS,
        )
        book = repository.orderbook_from_snapshot(snap)
        assert len(book.asks) == 2 and len(book.bids) == 1
