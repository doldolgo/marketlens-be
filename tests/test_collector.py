"""수집기 변환 로직 테스트 — _truncate / _to_rows / _usdt_amount (네트워크 불필요)."""

from __future__ import annotations

import math

import pytest

from app.exchanges.private.wallet_status import WalletStatus
from app.models.orderbook import MarketType, OrderBook, OrderBookLevel
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
