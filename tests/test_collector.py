"""수집기 변환 로직 테스트 — _truncate / _to_rows / _tops_to_rows / _usdt_amount

(네트워크 불필요)
"""

from __future__ import annotations

import math
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.db.models import PremiumArchive
from app.db.repository import SnapshotRow
from app.exchanges.private.wallet_status import WalletStatus
from app.models.bulk import BulkQuote
from app.models.orderbook import MarketType, OrderBook, OrderBookLevel
import app.services.collector_service as collector_module
from app.services.collector_service import CollectorService, _truncate

NOW_MS = 1_700_000_000_000


def levels(*pairs: tuple[float, float]) -> list[OrderBookLevel]:
    return [OrderBookLevel(price=p, size=s) for p, s in pairs]


def make_book(
    base: str = "BTC",
    quote: str = "KRW",
    *,
    bids=None,
    asks=None,
    exchange: str = "upbit",
) -> OrderBook:
    return OrderBook(
        exchange=exchange,
        symbol=f"{base}/{quote}",
        native_symbol=f"{quote}-{base}" if quote == "KRW" else f"{base}{quote}",
        market_type=MarketType.SPOT,
        base=base,
        quote=quote,
        bids=bids if bids is not None else levels((99.0, 1.0)),
        asks=asks if asks is not None else levels((101.0, 1.0)),
        timestamp=NOW_MS,
        latency_ms=1.0,
    )


class TestTruncate:
    """호가는 누적 체결 가능액이 max_amount 에 도달할 때까지만 저장한다."""

    def test_stops_after_covering_max_amount(self) -> None:
        # 단계당 100원어치 × 3단계. 150원을 커버하려면 2단계가 필요하다.
        out = _truncate(levels((100.0, 1.0), (101.0, 1.0), (102.0, 1.0)), 150.0)

        assert out == [[100.0, 1.0], [101.0, 1.0]]

    def test_exact_boundary_stops_at_that_level(self) -> None:
        out = _truncate(levels((100.0, 1.0), (101.0, 1.0)), 100.0)
        assert out == [[100.0, 1.0]]

    def test_keeps_all_when_book_is_shallower_than_max(self) -> None:
        out = _truncate(levels((100.0, 1.0), (101.0, 1.0)), 1_000_000.0)
        assert len(out) == 2

    def test_infinite_max_keeps_everything(self) -> None:
        out = _truncate(levels((100.0, 1.0), (101.0, 1.0)), float("inf"))
        assert len(out) == 2

    def test_empty_levels(self) -> None:
        assert _truncate([], 100.0) == []


class TestUsdtAmount:
    def setup_method(self) -> None:
        self.service = CollectorService()

    def test_converts_krw_to_usdt(self) -> None:
        assert self.service._usdt_amount(1_400_000.0, 1400.0) == pytest.approx(1000.0)

    def test_missing_rate_disables_truncation(self) -> None:
        assert math.isinf(self.service._usdt_amount(1_000_000.0, None))

    def test_non_positive_rate_disables_truncation(self) -> None:
        assert math.isinf(self.service._usdt_amount(1_000_000.0, 0.0))


class TestToRows:
    def setup_method(self) -> None:
        self.service = CollectorService()

    def rows(self, books, lasts, wallet=None, max_amount=float("inf")):
        return self.service._to_rows("upbit", books, lasts, wallet, max_amount)

    def test_uses_last_price_and_book_metadata(self) -> None:
        rows = self.rows({"BTC": make_book()}, {"BTC": 100.5})

        assert len(rows) == 1
        row = rows[0]
        assert row.exchange == "upbit"
        assert row.base == "BTC"
        assert row.quote == "KRW"
        assert row.native_symbol == "KRW-BTC"
        assert row.price == 100.5
        assert row.price_timestamp == NOW_MS
        assert row.asks == [[101.0, 1.0]]
        assert row.bids == [[99.0, 1.0]]

    def test_falls_back_to_mid_price_when_last_missing(self) -> None:
        rows = self.rows({"BTC": make_book()}, {})
        assert rows[0].price == pytest.approx(100.0)  # (99+101)/2

    def test_skips_coin_without_any_price(self) -> None:
        """체결가도 없고 호가도 비어 있으면(mid 불가) 저장하지 않는다."""
        book = make_book(bids=[], asks=[])
        assert self.rows({"BTC": book}, {}) == []

    def test_zero_last_price_falls_back_to_mid(self) -> None:
        """0 은 가격이 아니다 — 호가 중간값으로 대체된다."""
        rows = self.rows({"BTC": make_book()}, {"BTC": 0.0})
        assert rows[0].price == pytest.approx(100.0)

    def test_wallet_status_is_propagated(self) -> None:
        wallet = {"BTC": WalletStatus(deposit=True, withdrawal=False)}
        rows = self.rows({"BTC": make_book()}, {"BTC": 100.0}, wallet)

        assert rows[0].deposit_enabled is True
        assert rows[0].withdrawal_enabled is False

    def test_missing_wallet_entry_becomes_false(self) -> None:
        """지갑 목록에 그 코인이 없으면 null 이 아니라 보수적으로 False."""
        wallet = {"ETH": WalletStatus(deposit=True, withdrawal=True)}
        rows = self.rows({"BTC": make_book()}, {"BTC": 100.0}, wallet)

        assert rows[0].deposit_enabled is False
        assert rows[0].withdrawal_enabled is False

    def test_no_wallet_data_becomes_false(self) -> None:
        """지갑 조회 자체가 실패해도 null 을 두지 않는다."""
        rows = self.rows({"BTC": make_book()}, {"BTC": 100.0}, None)

        assert rows[0].deposit_enabled is False
        assert rows[0].withdrawal_enabled is False

    def test_orderbook_is_truncated_by_max_amount(self) -> None:
        book = make_book(
            asks=levels((100.0, 1.0), (101.0, 1.0), (102.0, 1.0)),
            bids=levels((99.0, 1.0), (98.0, 1.0), (97.0, 1.0)),
        )
        rows = self.rows({"BTC": book}, {"BTC": 100.0}, max_amount=150.0)

        assert rows[0].asks == [[100.0, 1.0], [101.0, 1.0]]
        assert rows[0].bids == [[99.0, 1.0], [98.0, 1.0]]

    def test_multiple_coins(self) -> None:
        books = {"BTC": make_book(), "ETH": make_book(base="ETH")}
        rows = self.rows(books, {"BTC": 100.0, "ETH": 50.0})
        assert {r.base for r in rows} == {"BTC", "ETH"}


def make_top(
    base: str = "BTC",
    *,
    bid: float | None = 99.0,
    ask: float | None = 101.0,
    bid_size: float | None = 2.0,
    ask_size: float | None = 3.0,
) -> BulkQuote:
    return BulkQuote(
        base=base,
        quote="USDT",
        native_symbol=f"{base}USDT",
        bid=bid,
        ask=ask,
        bid_size=bid_size,
        ask_size=ask_size,
    )


class TestTopsToRows:
    """bookTicker 일괄 조회 결과 → 1단계짜리 스냅샷 행."""

    NOW_S = 1_700_000_000

    def setup_method(self) -> None:
        self.service = CollectorService()

    def rows(self, tops, lasts, wallet=None):
        return self.service._tops_to_rows(
            "binance", tops, lasts, wallet, self.NOW_S
        )

    def test_builds_single_level_book_from_bulk_quote(self) -> None:
        rows = self.rows({"BTC": make_top()}, {"BTC": 100.5})

        assert len(rows) == 1
        row = rows[0]
        assert row.exchange == "binance"
        assert row.base == "BTC"
        assert row.quote == "USDT"
        assert row.native_symbol == "BTCUSDT"
        assert row.price == 100.5
        assert row.asks == [[101.0, 3.0]]
        assert row.bids == [[99.0, 2.0]]

    def test_price_timestamp_is_collect_time_in_ms(self) -> None:
        """bookTicker 에는 시각이 없다. OrderBook.timestamp 와 같은 단위(ms)로 맞춘다."""
        rows = self.rows({"BTC": make_top()}, {"BTC": 100.5})
        assert rows[0].price_timestamp == self.NOW_S * 1000

    def test_falls_back_to_mid_when_last_missing(self) -> None:
        rows = self.rows({"BTC": make_top()}, {})
        assert rows[0].price == pytest.approx(100.0)  # (99+101)/2

    def test_missing_size_becomes_zero_not_none(self) -> None:
        """잔량이 없어도 행 구조는 [가격, 잔량] 을 유지해야 한다 (liqDom/liqFx 계산용)."""
        rows = self.rows({"BTC": make_top(bid_size=None, ask_size=None)}, {"BTC": 100.0})

        assert rows[0].asks == [[101.0, 0.0]]
        assert rows[0].bids == [[99.0, 0.0]]

    def test_skips_quote_without_both_sides(self) -> None:
        tops = {"BTC": make_top(bid=None), "ETH": make_top("ETH", ask=None)}
        assert self.rows(tops, {"BTC": 100.0, "ETH": 50.0}) == []

    def test_skips_non_positive_quote(self) -> None:
        """거래가 없는 심볼은 호가가 0 으로 온다 — 가격으로 쓸 수 없다."""
        assert self.rows({"BTC": make_top(bid=0.0)}, {"BTC": 100.0}) == []

    def test_wallet_status_is_propagated(self) -> None:
        wallet = {"BTC": WalletStatus(deposit=True, withdrawal=False)}
        rows = self.rows({"BTC": make_top()}, {"BTC": 100.0}, wallet)

        assert rows[0].deposit_enabled is True
        assert rows[0].withdrawal_enabled is False

    def test_missing_wallet_data_becomes_false(self) -> None:
        rows = self.rows({"BTC": make_top()}, {"BTC": 100.0}, None)

        assert rows[0].deposit_enabled is False
        assert rows[0].withdrawal_enabled is False


def dom_row(
    exchange: str = "upbit",
    base: str = "BTC",
    *,
    bid: float = 140_000.0,
    deposit: bool = True,
) -> SnapshotRow:
    """국내 스냅샷 행 — 깊이 대상 선별에 필요한 필드만 채운다."""
    return SnapshotRow(
        exchange=exchange,
        base=base,
        native_symbol=f"KRW-{base}",
        quote="KRW",
        price=bid,
        bids=[[bid, 1.0]],
        asks=[[bid * 1.001, 1.0]],
        deposit_enabled=deposit,
    )


class TestSelectDepthTargets:
    """김프가 벌어졌고 '실제로 옮길 수 있는' 코인만 깊이를 조회한다."""

    RATE = 1400.0

    def setup_method(self) -> None:
        self.service = CollectorService()

    def select(self, domestic_rows, tops, wallet, rate=RATE):
        return self.service._select_depth_targets(domestic_rows, tops, rate, wallet)

    def case(self, *, fwd_percent: float, fx_wd: bool = True, dom_dep: bool = True):
        """원하는 김프율이 나오도록 국내 매수호가를 역산해 한 코인짜리 입력을 만든다."""
        ask = 100.0
        dom_bid = (1 + fwd_percent / 100) * ask * self.RATE
        return (
            {"upbit": {"BTC": dom_row(bid=dom_bid, deposit=dom_dep)}},
            {"BTC": make_top(ask=ask, bid=ask * 0.999)},
            {"BTC": WalletStatus(deposit=True, withdrawal=fx_wd)},
        )

    def test_selects_coin_above_threshold(self) -> None:
        assert self.select(*self.case(fwd_percent=2.0)) == ["BTC"]

    def test_skips_premium_below_threshold(self) -> None:
        """기본 하한 1.0% 미만이면 슬리피지를 계산할 이유가 없다."""
        assert self.select(*self.case(fwd_percent=0.5)) == []

    def test_skips_when_binance_withdrawal_closed(self) -> None:
        """해외에서 출금이 막히면 김프가 아무리 커도 옮길 수 없다."""
        assert self.select(*self.case(fwd_percent=10.0, fx_wd=False)) == []

    def test_skips_when_domestic_deposit_closed(self) -> None:
        """국내 입금이 막힌 경우 — 김프가 크게 벌어지는 전형적 원인이다."""
        assert self.select(*self.case(fwd_percent=10.0, dom_dep=False)) == []

    def test_caps_at_max_count_and_orders_by_premium(self) -> None:
        """후보 20개면 상한 12개로 잘리고, 김프 높은 순이어야 한다."""
        ask = 100.0
        dom, tops, wallet = {"upbit": {}}, {}, {}
        for i in range(20):
            base = f"C{i:02d}"
            fwd = 1.0 + i  # C19 가 가장 높다
            dom["upbit"][base] = dom_row(base=base, bid=(1 + fwd / 100) * ask * self.RATE)
            tops[base] = make_top(base, ask=ask, bid=ask * 0.999)
            wallet[base] = WalletStatus(deposit=True, withdrawal=True)

        out = self.select(dom, tops, wallet)

        assert len(out) == 12
        assert out == [f"C{i:02d}" for i in range(19, 7, -1)]

    def test_no_rate_selects_nothing(self) -> None:
        """환율이 없으면 김프를 계산할 수 없다 — 조용히 건너뛴다."""
        assert self.select(*self.case(fwd_percent=10.0), rate=None) == []

    def test_uses_highest_domestic_bid_across_exchanges(self) -> None:
        """국내 여러 곳 중 가장 유리한 매도처(최고 매수호가)를 기준으로 판단한다."""
        ask = 100.0
        low = 1.005 * ask * self.RATE   # 0.5% — 단독이면 탈락
        high = 1.03 * ask * self.RATE   # 3.0% — 이걸 써야 선정
        dom = {
            "upbit": {"BTC": dom_row("upbit", bid=low)},
            "bithumb": {"BTC": dom_row("bithumb", bid=high)},
        }
        tops = {"BTC": make_top(ask=ask, bid=ask * 0.999)}
        wallet = {"BTC": WalletStatus(deposit=True, withdrawal=True)}

        assert self.select(dom, tops, wallet) == ["BTC"]


class TestApplyDepth:
    """선정된 코인만 깊은 호가로 덮어쓴다. 실패는 기록하되 수집을 막지 않는다."""

    def setup_method(self) -> None:
        self.service = CollectorService()

    def binance_row(self) -> SnapshotRow:
        """일괄 조회가 만든 1단계짜리 행."""
        return SnapshotRow(
            exchange="binance",
            base="BTC",
            native_symbol="BTCUSDT",
            quote="USDT",
            price=100.0,
            asks=[[101.0, 1.0]],
            bids=[[99.0, 1.0]],
        )

    def patch_exchange(self, monkeypatch, fetch) -> None:
        monkeypatch.setattr(
            collector_module,
            "get_exchange",
            lambda _eid: SimpleNamespace(fetch_orderbook=fetch),
        )

    async def test_overwrites_with_truncated_depth(self, monkeypatch) -> None:
        deep = make_book(
            base="BTC",
            quote="USDT",
            exchange="binance",
            asks=levels((101.0, 1.0), (102.0, 1.0), (103.0, 1.0)),
            bids=levels((99.0, 1.0), (98.0, 1.0), (97.0, 1.0)),
        )

        async def fetch(*_a, **_kw):
            return deep

        self.patch_exchange(monkeypatch, fetch)
        rows = {"BTC": self.binance_row()}
        failures: list = []

        # 1,000,000 KRW ÷ 환율 1400 ≈ 714 USDT → 101 원어치씩이면 전 단계를 다 담는다
        applied = await self.service._apply_depth(["BTC"], rows, 1400.0, failures)

        assert applied == 1
        assert failures == []
        assert rows["BTC"].asks == [[101.0, 1.0], [102.0, 1.0], [103.0, 1.0]]
        assert rows["BTC"].bids == [[99.0, 1.0], [98.0, 1.0], [97.0, 1.0]]

    async def test_failure_is_recorded_and_row_kept(self, monkeypatch) -> None:
        """깊이를 못 받아도 일괄 조회로 얻은 최우선 호가는 살아 있어야 한다."""

        async def fetch(*_a, **_kw):
            raise RuntimeError("boom")

        self.patch_exchange(monkeypatch, fetch)
        rows = {"BTC": self.binance_row()}
        failures: list = []

        applied = await self.service._apply_depth(["BTC"], rows, 1400.0, failures)

        assert applied == 0
        assert len(failures) == 1
        assert failures[0].sym == "BTC"
        assert rows["BTC"].asks == [[101.0, 1.0]]

    async def test_empty_book_does_not_wipe_top_of_book(self, monkeypatch) -> None:
        empty = make_book(
            base="BTC", quote="USDT", exchange="binance", asks=[], bids=[]
        )

        async def fetch(*_a, **_kw):
            return empty

        self.patch_exchange(monkeypatch, fetch)
        rows = {"BTC": self.binance_row()}

        applied = await self.service._apply_depth(["BTC"], rows, 1400.0, [])

        assert applied == 0
        assert rows["BTC"].asks == [[101.0, 1.0]]
        assert rows["BTC"].bids == [[99.0, 1.0]]


class TestArchiveThrottle:
    """premium_archive 는 라이브 수집보다 느리게 적재한다 (3-4 주기 가드)."""

    async def _archive_count(self, db) -> int:
        return (
            await db.execute(select(func.count()).select_from(PremiumArchive))
        ).scalar_one()

    async def _refresh_once(self, service, db, monkeypatch) -> None:
        """거래소 호출을 전부 대체해 수집 사이클 한 번을 돌린다."""
        top = make_top(ask=100.0, bid=99.9)

        async def domestic(eid, failures):
            return eid, {"BTC": make_book(exchange=eid)}, {"BTC": 140_000.0}

        async def binance(bases, failures):
            return "binance", {"BTC": top}, {"BTC": 100.0}

        async def futures(warnings):
            return 1

        async def wallet(eid, warnings):
            return {"BTC": WalletStatus(deposit=True, withdrawal=True)}

        async def rate(failures):
            return SimpleNamespace(rate=1400.0, ts=1_700_000_000, round_no=1)

        monkeypatch.setattr(service, "_domestic_market", domestic)
        monkeypatch.setattr(service, "_binance_market", binance)
        monkeypatch.setattr(service, "_binance_futures_count", futures)
        monkeypatch.setattr(service, "_wallet", wallet)
        monkeypatch.setattr(service, "_usdkrw_rate", rate)
        await service.refresh(db)

    async def test_archives_once_within_the_interval(self, db, monkeypatch) -> None:
        """60초 안에 여러 번 수집해도 적재는 첫 회차 한 번만 일어난다."""
        monkeypatch.setattr(settings, "archive_interval_seconds", 60.0)
        service = CollectorService()

        await self._refresh_once(service, db, monkeypatch)
        after_first = await self._archive_count(db)
        assert after_first > 0, "첫 사이클은 적재해야 한다"

        for _ in range(3):
            await self._refresh_once(service, db, monkeypatch)

        assert await self._archive_count(db) == after_first

    async def test_archives_again_after_interval_elapses(self, db, monkeypatch) -> None:
        """주기가 지나면 다시 적재한다.

        premium_archive 의 PK 는 (dom, fx, base, ts) 이고 ts 는 **초** 단위라,
        같은 초에 두 번 적재하면 UPSERT 로 한 행이 된다 — 벽시계도 같이 밀어야
        '다시 적재됐다'를 행 수로 확인할 수 있다.
        """
        monkeypatch.setattr(settings, "archive_interval_seconds", 60.0)
        service = CollectorService()

        await self._refresh_once(service, db, monkeypatch)
        after_first = await self._archive_count(db)

        # 마지막 적재 시각을 과거로 밀어 주기가 지난 상황을 만든다
        service._last_archive_ts -= 61.0
        real_time = time.time  # 패치 전 원본을 잡아둔다 (안 그러면 자기 자신을 부른다)
        monkeypatch.setattr(collector_module.time, "time", lambda: real_time() + 61.0)
        await self._refresh_once(service, db, monkeypatch)

        assert await self._archive_count(db) > after_first


class TestWalletCache:
    """입출금 상태는 wallet_refresh_seconds 주기로만 새로 받는다 (3-4)."""

    async def test_reuses_cache_within_the_interval(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "wallet_refresh_seconds", 60.0)
        service = CollectorService()
        calls: list[str] = []

        async def wallet(eid, warnings):
            calls.append(eid)
            return {"BTC": WalletStatus(deposit=True, withdrawal=True)}

        monkeypatch.setattr(service, "_wallet", wallet)

        # 1회차: 세 거래소 모두 실제 조회
        service._wallet_cache = {}
        await self._cycle(service)
        assert len(calls) == 3

        # 2·3회차: 캐시를 읽으므로 추가 호출이 없어야 한다
        await self._cycle(service)
        await self._cycle(service)
        assert len(calls) == 3

    async def _cycle(self, service) -> None:
        """_refresh 의 1단계 wallet 분기만 떼어 흉내낸다."""
        cycle_ts = time.monotonic()
        refresh_wallet = (
            cycle_ts - service._last_wallet_ts >= settings.wallet_refresh_seconds
            or not service._wallet_cache
        )
        if refresh_wallet:
            results = [await service._wallet(eid, []) for eid in ("upbit", "bithumb", "binance")]
            service._wallet_cache = dict(zip(("upbit", "bithumb", "binance"), results))
            service._last_wallet_ts = cycle_ts
