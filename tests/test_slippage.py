"""슬리피지 계산 테스트 (네트워크 불필요)."""

from __future__ import annotations

import pytest

from app.core.errors import InvalidRequestError, MarketDataNotFoundError
from app.models.orderbook import MarketType, OrderBook, OrderBookLevel
from app.models.slippage import OrderSide
from app.services.orderbook_walk import walk_by_amount, walk_by_quantity
from app.services.slippage_service import SlippageService


def levels(*pairs: tuple[float, float]) -> list[OrderBookLevel]:
    return [OrderBookLevel(price=p, size=s) for p, s in pairs]


def make_book(bids, asks) -> OrderBook:
    return OrderBook(
        exchange="upbit",
        symbol="BTC/KRW",
        native_symbol="KRW-BTC",
        market_type=MarketType.SPOT,
        base="BTC",
        quote="KRW",
        bids=bids,
        asks=asks,
        timestamp=1700000000000,
        latency_ms=1.0,
    )


class TestWalkByQuantityForBuy:
    """수량 기준으로도 매수를 계산할 수 있어야 한다 (기존엔 금액 기준만)."""

    def test_buy_by_quantity(self) -> None:
        r = walk_by_quantity(levels((100.0, 1.0), (120.0, 5.0)), quantity=2.0)

        assert r.amount == pytest.approx(220.0)  # 100×1 + 120×1
        assert r.average_price == pytest.approx(110.0)
        assert r.levels_consumed == 2

    def test_sell_by_amount(self) -> None:
        """매도도 금액 기준으로 계산 가능 — '300원어치 팔면?'"""
        r = walk_by_amount(levels((100.0, 10.0)), amount=300.0)

        assert r.quantity == pytest.approx(3.0)
        assert r.amount == pytest.approx(300.0)


class TestFills:
    """단계별 체결 내역 — 업비트 툴팁의 평균가/누적량/누적액과 같은 값."""

    def setup_method(self) -> None:
        self.service = SlippageService()

    def test_fills_track_cumulative_values(self) -> None:
        walk = walk_by_amount(levels((100.0, 1.0), (110.0, 1.0), (120.0, 10.0)), 320.0)
        fills = self.service._build_fills(walk)

        assert [f.level for f in fills] == [1, 2, 3]
        assert fills[0].cumulative_quantity == pytest.approx(1.0)
        assert fills[1].cumulative_quantity == pytest.approx(2.0)
        assert fills[1].cumulative_amount == pytest.approx(210.0)
        assert fills[1].cumulative_average == pytest.approx(105.0)

    def test_cumulative_average_equals_amount_over_quantity(self) -> None:
        """업비트 툴팁의 검산식: 평균가 × 누적량 = 누적액."""
        walk = walk_by_amount(levels((100.0, 1.0), (110.0, 2.0)), 250.0)
        for f in self.service._build_fills(walk):
            assert f.cumulative_average * f.cumulative_quantity == pytest.approx(
                f.cumulative_amount
            )

    def test_last_fill_can_be_partial(self) -> None:
        walk = walk_by_amount(levels((100.0, 10.0)), 250.0)
        fills = self.service._build_fills(walk)

        assert len(fills) == 1
        assert fills[0].size == pytest.approx(2.5)  # 잔량 10 중 2.5 만 체결
        assert fills[0].amount == pytest.approx(250.0)


class TestSlippageComputation:
    def setup_method(self) -> None:
        self.service = SlippageService()

    def compute(self, side, *, amount=None, quantity=None, book=None):
        book = book or make_book(
            bids=levels((90.0, 1.0), (80.0, 10.0)),
            asks=levels((100.0, 1.0), (120.0, 10.0)),
        )
        return self.service._compute(book, side, amount=amount, quantity=quantity)

    def test_single_level_has_zero_slippage(self) -> None:
        r = self.compute(OrderSide.BUY, amount=50.0)  # 1단계(100원×1개=100원) 안

        assert r.slippage_percent == pytest.approx(0.0)
        assert r.levels_consumed == 1
        assert r.average_price == pytest.approx(r.best_price)

    def test_buy_slippage_grows_beyond_first_level(self) -> None:
        r = self.compute(OrderSide.BUY, amount=220.0)  # 100×1 + 120×1

        assert r.best_price == 100.0
        assert r.average_price == pytest.approx(110.0)
        assert r.slippage_percent == pytest.approx(10.0)
        assert r.levels_consumed == 2

    def test_sell_slippage_is_positive_too(self) -> None:
        r = self.compute(OrderSide.SELL, quantity=2.0)  # 90×1 + 80×1 → 평균 85

        assert r.best_price == 90.0
        assert r.average_price == pytest.approx(85.0)
        assert r.slippage_percent == pytest.approx(100 / 18)  # (90-85)/90 ≈ 5.56%

    def test_buy_slippage_cost_is_missed_coins_in_money(self) -> None:
        """220원으로 최우선 호가라면 2.2개, 실제로는 2개 → 0.2개 손해."""
        r = self.compute(OrderSide.BUY, amount=220.0)

        assert r.quantity == pytest.approx(2.0)
        assert r.slippage_cost == pytest.approx(0.2 * 110.0)

    def test_sell_slippage_cost_is_missed_proceeds(self) -> None:
        """2개를 최우선 호가로 팔면 180원, 실제로는 170원 → 10원 손해."""
        r = self.compute(OrderSide.SELL, quantity=2.0)

        assert r.amount == pytest.approx(170.0)
        assert r.slippage_cost == pytest.approx(10.0)

    def test_slippage_cost_never_negative(self) -> None:
        r = self.compute(OrderSide.BUY, amount=50.0)
        assert r.slippage_cost >= 0.0

    def test_top_level_amount_is_the_zero_slippage_threshold(self) -> None:
        r = self.compute(OrderSide.BUY, amount=50.0)
        assert r.top_level_amount == pytest.approx(100.0)  # 100원 × 1개

    def test_depth_exhausted_is_flagged(self) -> None:
        r = self.compute(OrderSide.BUY, amount=1_000_000.0)

        assert r.depth_exhausted is True
        assert any("소진" in w for w in r.warnings)

    def test_empty_side_raises(self) -> None:
        """저장된 호가가 비면 404 — 거래소 호출이 없으니 502 가 아니다."""
        book = make_book(bids=[], asks=levels((100.0, 1.0)))
        with pytest.raises(MarketDataNotFoundError):
            self.compute(OrderSide.SELL, quantity=1.0, book=book)

    def test_timing_slippage_warning_is_always_present(self) -> None:
        r = self.compute(OrderSide.BUY, amount=50.0)
        assert any("타이밍 슬리피지" in w for w in r.warnings)


class TestInputValidation:
    def setup_method(self) -> None:
        self.service = SlippageService()

    def test_both_inputs_rejected(self) -> None:
        import asyncio

        from app.models.symbol import Symbol

        with pytest.raises(InvalidRequestError):
            asyncio.run(
                self.service.calculate(
                    None,  # 검증은 DB 를 읽기 전에 일어나므로 세션이 필요 없다
                    "upbit",
                    Symbol("BTC", "KRW"),
                    side=OrderSide.BUY,
                    amount=100.0,
                    quantity=1.0,
                )
            )

    def test_neither_input_rejected(self) -> None:
        import asyncio

        from app.models.symbol import Symbol

        with pytest.raises(InvalidRequestError):
            asyncio.run(
                self.service.calculate(
                    None, "upbit", Symbol("BTC", "KRW"), side=OrderSide.BUY
                )
            )

    def test_side_values_are_url_friendly(self) -> None:
        assert OrderSide.BUY.value == "buy"
        assert OrderSide.SELL.value == "sell"
