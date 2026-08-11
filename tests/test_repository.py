"""DB repository 테스트 — in-memory SQLite 로 UPSERT/삭제/조회를 검증한다."""

from __future__ import annotations

import pytest
from conftest import NOW_MS, seed_rates, snapshot_row

from app.core.errors import MarketDataNotFoundError
from app.db import repository
from app.db.models import MarketSnapshot
from app.models.orderbook import MarketType


class TestReplaceExchangeSnapshots:
    async def test_inserts_rows(self, db) -> None:
        saved, deleted = await repository.replace_exchange_snapshots(
            db,
            "upbit",
            [
                snapshot_row("upbit", "BTC", 100_000_000.0),
                snapshot_row("upbit", "ETH", 5_000_000.0),
            ],
        )
        await db.commit()

        assert (saved, deleted) == (2, 0)
        snaps = await repository.get_snapshots(db, exchange="upbit")
        assert {s.base for s in snaps} == {"BTC", "ETH"}

    async def test_upserts_existing_and_deletes_missing(self, db) -> None:
        """있던 코인은 갱신되고, 이번 수집에 없는 코인(상장폐지)은 지워진다."""
        await repository.replace_exchange_snapshots(
            db,
            "upbit",
            [
                snapshot_row("upbit", "BTC", 100_000_000.0),
                snapshot_row("upbit", "DOGE", 300.0),
            ],
        )
        await db.commit()

        saved, deleted = await repository.replace_exchange_snapshots(
            db,
            "upbit",
            [snapshot_row("upbit", "BTC", 101_000_000.0, deposit=False)],
        )
        await db.commit()

        assert (saved, deleted) == (1, 1)
        snaps = await repository.get_snapshots(db, exchange="upbit")
        assert [s.base for s in snaps] == ["BTC"]
        assert snaps[0].price == 101_000_000.0  # UPSERT 로 가격이 갱신됐다
        assert snaps[0].deposit_enabled is False  # 입출금 플래그도 갱신됐다

    async def test_empty_rows_deletes_whole_exchange_only(self, db) -> None:
        """빈 수집 결과는 그 거래소 행 전체를 지우되 다른 거래소는 건드리지 않는다."""
        await repository.replace_exchange_snapshots(
            db, "upbit", [snapshot_row("upbit", "BTC", 100_000_000.0)]
        )
        await repository.replace_exchange_snapshots(
            db,
            "binance",
            [snapshot_row("binance", "BTC", 71_000.0, quote="USDT")],
        )
        await db.commit()

        saved, deleted = await repository.replace_exchange_snapshots(db, "upbit", [])
        await db.commit()

        assert (saved, deleted) == (0, 1)
        assert await repository.get_snapshots(db, exchange="upbit") == []
        assert len(await repository.get_snapshots(db, exchange="binance")) == 1


class TestKrwRate:
    async def test_insert_then_update(self, db) -> None:
        await repository.upsert_krw_rate(
            db,
            exchange="upbit",
            rate=1400.0,
            native_symbol="KRW-USDT",
            price_timestamp=NOW_MS,
        )
        await db.commit()
        await repository.upsert_krw_rate(
            db,
            exchange="upbit",
            rate=1410.0,
            native_symbol="KRW-USDT",
            price_timestamp=NOW_MS + 1,
        )
        await db.commit()

        rates = await repository.get_krw_rates(db)
        assert len(rates) == 1  # 거래소당 한 행
        assert rates[0].rate == 1410.0
        assert rates[0].price_timestamp == NOW_MS + 1

    async def test_multiple_exchanges_have_separate_rows(self, db) -> None:
        await seed_rates(db, {"upbit": 1400.0, "bithumb": 1401.0})

        by_exchange = {r.exchange: r.rate for r in await repository.get_krw_rates(db)}
        assert by_exchange == {"upbit": 1400.0, "bithumb": 1401.0}

    async def test_require_krw_rate_raises_when_missing(self, db) -> None:
        with pytest.raises(MarketDataNotFoundError):
            await repository.require_krw_rate(db, "upbit")


class TestQueries:
    async def _seed(self, db) -> None:
        await repository.replace_exchange_snapshots(
            db,
            "upbit",
            [
                snapshot_row("upbit", "BTC", 100_000_000.0),
                snapshot_row("upbit", "ETH", 5_000_000.0),
            ],
        )
        await repository.replace_exchange_snapshots(
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
