"""호가창 소진 계산과 차익 시뮬레이션 테스트 (네트워크 불필요)."""

from __future__ import annotations

import pytest

from app.core.errors import ExchangeAPIError
from app.models.orderbook import MarketType, OrderBook, OrderBookLevel
from app.services.arbitrage_service import ArbitrageService
from app.services.fx import FxRate
from app.services.orderbook_walk import walk_buy, walk_sell

FX = FxRate(rate=1000.0, source="test", timestamp=1700000000000)


def levels(*pairs: tuple[float, float]) -> list[OrderBookLevel]:
    return [OrderBookLevel(price=p, size=s) for p, s in pairs]


def make_book(exchange: str, quote: str, bids, asks) -> OrderBook:
    return OrderBook(
        exchange=exchange,
        symbol=f"BTC/{quote}",
        native_symbol=f"{exchange}-BTC",
        market_type=MarketType.SPOT,
        base="BTC",
        quote=quote,
        bids=bids,
        asks=asks,
        timestamp=1700000000000,
        latency_ms=1.0,
    )


class TestWalkBuy:
    def test_single_level_partial_fill(self) -> None:
        # 100원짜리 10개 중 3개어치(300원)만 산다
        r = walk_buy(levels((100.0, 10.0)), budget=300.0)

        assert r.quantity == pytest.approx(3.0)
        assert r.amount == pytest.approx(300.0)
        assert r.levels_consumed == 1
        assert r.exhausted is False
        assert r.average_price == pytest.approx(100.0)

    def test_walks_multiple_levels(self) -> None:
        # 100원×1개(100원) 전부 + 110원에서 나머지 100원어치
        r = walk_buy(levels((100.0, 1.0), (110.0, 5.0)), budget=200.0)

        assert r.amount == pytest.approx(200.0)
        assert r.quantity == pytest.approx(1.0 + 100.0 / 110.0)
        assert r.levels_consumed == 2
        assert r.average_price > 100.0        # 평균가가 최우선 호가보다 불리해짐

    def test_exhausted_when_budget_exceeds_book(self) -> None:
        r = walk_buy(levels((100.0, 1.0), (110.0, 1.0)), budget=10_000.0)

        assert r.exhausted is True
        assert r.quantity == pytest.approx(2.0)
        assert r.amount == pytest.approx(210.0)   # 예산이 아니라 실제 체결액

    def test_exact_level_boundary(self) -> None:
        """예산이 한 단계 잔량과 정확히 같으면 그 단계에서 끝난다."""
        r = walk_buy(levels((100.0, 2.0), (110.0, 1.0)), budget=200.0)

        assert r.quantity == pytest.approx(2.0)
        assert r.levels_consumed == 1
        assert r.exhausted is False

    def test_zero_budget(self) -> None:
        r = walk_buy(levels((100.0, 1.0)), budget=0.0)
        assert r.quantity == 0.0 and r.exhausted is False

    def test_empty_book_is_exhausted(self) -> None:
        r = walk_buy([], budget=100.0)
        assert r.exhausted is True and r.quantity == 0.0


class TestWalkSell:
    def test_single_level_partial_fill(self) -> None:
        r = walk_sell(levels((100.0, 10.0)), quantity=3.0)

        assert r.amount == pytest.approx(300.0)
        assert r.quantity == pytest.approx(3.0)
        assert r.exhausted is False

    def test_walks_down_the_book(self) -> None:
        # 100원에 1개, 90원에 1개 → 190원
        r = walk_sell(levels((100.0, 1.0), (90.0, 5.0)), quantity=2.0)

        assert r.amount == pytest.approx(190.0)
        assert r.average_price == pytest.approx(95.0)
        assert r.levels_consumed == 2

    def test_exhausted_when_quantity_exceeds_book(self) -> None:
        r = walk_sell(levels((100.0, 1.0)), quantity=5.0)

        assert r.exhausted is True
        assert r.quantity == pytest.approx(1.0)   # 실제로 팔린 수량만
        assert r.amount == pytest.approx(100.0)


class TestSlippage:
    def test_no_slippage_on_single_level(self) -> None:
        r = walk_buy(levels((100.0, 10.0)), budget=100.0)
        assert r.slippage_percent(100.0, is_buy=True) == pytest.approx(0.0)

    def test_buy_slippage_is_positive(self) -> None:
        r = walk_buy(levels((100.0, 1.0), (120.0, 10.0)), budget=220.0)
        # 평균 110원 vs 최우선 100원 → +10%
        assert r.slippage_percent(100.0, is_buy=True) == pytest.approx(10.0)

    def test_sell_slippage_is_also_positive(self) -> None:
        """매도는 평균가가 낮아질수록 불리하다. 부호를 뒤집어 양수로 만든다."""
        r = walk_sell(levels((100.0, 1.0), (80.0, 1.0)), quantity=2.0)
        # 평균 90원 vs 최우선 100원 → +10% 불리
        assert r.slippage_percent(100.0, is_buy=False) == pytest.approx(10.0)

    def test_never_returns_negative_zero(self) -> None:
        r = walk_sell(levels((100.0, 10.0)), quantity=1.0)
        assert r.slippage_percent(100.0, is_buy=False) == 0.0


class TestVenueSelection:
    def setup_method(self) -> None:
        self.service = ArbitrageService()

    def test_converts_usdt_book_to_krw(self) -> None:
        book = make_book("binance", "USDT", levels((100.0, 1.0)), levels((101.0, 1.0)))
        venue = self.service._to_venue(book, FX)

        assert venue.best_bid_krw == pytest.approx(100_000.0)   # × 환율 1000
        assert venue.best_ask_krw == pytest.approx(101_000.0)

    def test_krw_book_is_not_converted(self) -> None:
        book = make_book("upbit", "KRW", levels((100_000.0, 1.0)), levels((101_000.0, 1.0)))
        venue = self.service._to_venue(book, FX)

        assert venue.best_bid_krw == pytest.approx(100_000.0)

    def test_empty_book_raises(self) -> None:
        with pytest.raises(ExchangeAPIError):
            self.service._to_venue(make_book("upbit", "KRW", [], []), FX)

    def test_unsupported_quote_raises(self) -> None:
        with pytest.raises(ExchangeAPIError):
            self.service._to_krw_factor("ETH", FX)

    def test_krw_levels_are_scaled(self) -> None:
        scaled = self.service._krw_levels(levels((100.0, 2.0), (101.0, 3.0)), 1000.0)

        assert [lv.price for lv in scaled] == [100_000.0, 101_000.0]
        assert [lv.size for lv in scaled] == [2.0, 3.0]     # 수량은 그대로

    def test_krw_levels_returns_same_object_when_factor_is_one(self) -> None:
        original = levels((100.0, 1.0))
        assert self.service._krw_levels(original, 1.0) is original


class TestProfitMath:
    """차익 계산이 손으로 계산한 값과 맞는지 확인한다."""

    def test_end_to_end_numbers(self) -> None:
        service = ArbitrageService()

        # 싼 곳: 100,000원에 무제한 / 비싼 곳: 110,000원에 무제한
        cheap = make_book("upbit", "KRW",
                          levels((99_000.0, 100.0)), levels((100_000.0, 100.0)))
        pricey = make_book("binance", "USDT",
                           levels((110.0, 100.0)), levels((111.0, 100.0)))   # ×1000 환산

        buy_walk = walk_buy(service._krw_levels(cheap.asks, 1.0), 1_000_000.0)
        sell_walk = walk_sell(service._krw_levels(pricey.bids, 1000.0), buy_walk.quantity)

        assert buy_walk.quantity == pytest.approx(10.0)          # 100만원 / 10만원
        assert sell_walk.amount == pytest.approx(1_100_000.0)    # 10개 × 11만원

        profit = sell_walk.amount - buy_walk.amount
        assert profit == pytest.approx(100_000.0)
        assert profit / buy_walk.amount * 100 == pytest.approx(10.0)
