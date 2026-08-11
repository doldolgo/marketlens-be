"""호가창 소진(walk) 순수 계산 테스트 (네트워크·DB 불필요)."""

from __future__ import annotations

import pytest

from app.models.orderbook import OrderBookLevel
from app.services.orderbook_walk import (
    market_price,
    walk_buy,
    walk_by_amount,
    walk_by_quantity,
    walk_sell,
)


def levels(*pairs: tuple[float, float]) -> list[OrderBookLevel]:
    return [OrderBookLevel(price=p, size=s) for p, s in pairs]


class TestWalkBuy:
    def test_single_level_partial_fill(self) -> None:
        # 100원짜리 10개 중 3개어치(300원)만 산다
        r = walk_buy(levels((100.0, 10.0)), amount=300.0)

        assert r.quantity == pytest.approx(3.0)
        assert r.amount == pytest.approx(300.0)
        assert r.levels_consumed == 1
        assert r.exhausted is False
        assert r.average_price == pytest.approx(100.0)

    def test_walks_multiple_levels(self) -> None:
        # 100원×1개(100원) 전부 + 110원에서 나머지 100원어치
        r = walk_buy(levels((100.0, 1.0), (110.0, 5.0)), amount=200.0)

        assert r.amount == pytest.approx(200.0)
        assert r.quantity == pytest.approx(1.0 + 100.0 / 110.0)
        assert r.levels_consumed == 2
        assert r.average_price > 100.0  # 평균가가 최우선 호가보다 불리해짐

    def test_exhausted_when_budget_exceeds_book(self) -> None:
        r = walk_buy(levels((100.0, 1.0), (110.0, 1.0)), amount=10_000.0)

        assert r.exhausted is True
        assert r.quantity == pytest.approx(2.0)
        assert r.amount == pytest.approx(210.0)  # 예산이 아니라 실제 체결액

    def test_exact_level_boundary(self) -> None:
        """예산이 한 단계 잔량과 정확히 같으면 그 단계에서 끝난다."""
        r = walk_buy(levels((100.0, 2.0), (110.0, 1.0)), amount=200.0)

        assert r.quantity == pytest.approx(2.0)
        assert r.levels_consumed == 1
        assert r.exhausted is False

    def test_zero_budget(self) -> None:
        r = walk_buy(levels((100.0, 1.0)), amount=0.0)
        assert r.quantity == 0.0 and r.exhausted is False

    def test_empty_book_is_exhausted(self) -> None:
        r = walk_buy([], amount=100.0)
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
        assert r.quantity == pytest.approx(1.0)  # 실제로 팔린 수량만
        assert r.amount == pytest.approx(100.0)


class TestAliasesAndCrossUse:
    """매수를 수량 기준으로, 매도를 금액 기준으로도 계산할 수 있어야 한다."""

    def test_walk_aliases(self) -> None:
        assert walk_buy is walk_by_amount
        assert walk_sell is walk_by_quantity

    def test_buy_by_quantity(self) -> None:
        r = walk_by_quantity(levels((100.0, 1.0), (120.0, 5.0)), quantity=2.0)

        assert r.amount == pytest.approx(220.0)  # 100×1 + 120×1
        assert r.average_price == pytest.approx(110.0)
        assert r.levels_consumed == 2

    def test_sell_by_amount(self) -> None:
        """'300원어치 팔면?' — 금액 기준 매도."""
        r = walk_by_amount(levels((100.0, 10.0)), amount=300.0)

        assert r.quantity == pytest.approx(3.0)
        assert r.amount == pytest.approx(300.0)


class TestSlippagePercent:
    def test_no_slippage_on_single_level(self) -> None:
        r = walk_buy(levels((100.0, 10.0)), amount=100.0)
        assert r.slippage_percent(100.0, is_buy=True) == pytest.approx(0.0)

    def test_buy_slippage_is_positive(self) -> None:
        r = walk_buy(levels((100.0, 1.0), (120.0, 10.0)), amount=220.0)
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


class TestMarketPrice:
    """시장가 = 금액이 체결을 끝내는 지점을 asks·bids 양쪽에서 잡아 평균."""

    def test_within_first_level_equals_mid(self) -> None:
        """1단계 안에서 끝나면 중간가와 같다 — 중간가는 이 정의의 특수한 경우."""
        bids = levels((99.0, 10.0))
        asks = levels((101.0, 10.0))

        assert market_price(bids, asks, 100.0) == pytest.approx(100.0)  # (99+101)/2

    def test_larger_amount_pushes_both_ends_outward(self) -> None:
        bids = levels((99.0, 1.0), (90.0, 10.0))
        asks = levels((101.0, 1.0), (110.0, 10.0))

        small = market_price(bids, asks, 50.0)  # 1단계 안 → (99+101)/2 = 100
        large = market_price(bids, asks, 500.0)  # 2단계까지 → (90+110)/2 = 100

        assert small == pytest.approx(100.0)
        assert large == pytest.approx(100.0)  # 대칭 호가라 중심은 유지

    def test_asymmetric_book_shifts_market_price(self) -> None:
        """매도 쪽만 얇으면 시장가가 위로 밀린다."""
        bids = levels((99.0, 100.0))  # 두껍다
        asks = levels((101.0, 0.1), (130.0, 100.0))  # 얇다

        assert market_price(bids, asks, 50.0) > 100.0  # (99+130)/2

    def test_returns_none_on_empty_side(self) -> None:
        lv = levels((100.0, 1.0))
        assert market_price([], lv, 10.0) is None
        assert market_price(lv, [], 10.0) is None

    def test_zero_amount_returns_none(self) -> None:
        lv = levels((100.0, 1.0))
        assert market_price(lv, lv, 0.0) is None
