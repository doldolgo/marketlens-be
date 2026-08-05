"""호가창 소진(order book walk) 계산.

시장가 주문은 최우선 호가 하나로 전부 체결되지 않는다. 그 단계의 잔량을 다 먹으면
다음 단계로 넘어가고, 그럴수록 가격이 불리해진다. 이 모듈은 그 과정을 그대로
계산해 **평균 체결가**와 **슬리피지**를 구한다.

    매수: 예산을 다 쓸 때까지 asks 를 위에서부터 훑는다 → 코인 수량이 나온다
    매도: 수량을 다 팔 때까지 bids 를 위에서부터 훑는다 → 금액이 나온다
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.orderbook import OrderBookLevel


@dataclass(frozen=True, slots=True)
class WalkResult:
    """호가창을 훑은 결과."""

    #: 체결된 코인 수량
    quantity: float
    #: 오간 금액 (매수면 지출, 매도면 수령)
    amount: float
    #: 소진한 호가 단계 수
    levels_consumed: int
    #: 호가가 부족해 요청을 다 채우지 못했는지
    exhausted: bool

    @property
    def average_price(self) -> float:
        """평균 체결가. 체결이 없으면 0."""
        return self.amount / self.quantity if self.quantity > 0 else 0.0

    def slippage_percent(self, best_price: float, *, is_buy: bool) -> float:
        """최우선 호가 대비 평균 체결가가 얼마나 불리해졌는지 (%).

        매수는 평균가가 높을수록, 매도는 낮을수록 불리하다.
        어느 쪽이든 **0 이상**이 나오도록 부호를 맞춘다.
        """
        if best_price <= 0 or self.quantity <= 0:
            return 0.0

        diff = self.average_price - best_price
        if not is_buy:
            diff = -diff

        # 호가가 정렬되어 있으므로 수학적으로 음수가 나올 수 없다.
        # 부동소수점 오차로 생기는 -0.0 / 미세 음수만 0 으로 다듬는다.
        return max(0.0, diff / best_price * 100)


def walk_buy(asks: list[OrderBookLevel], budget: float) -> WalkResult:
    """예산으로 매도호가를 훑어 살 수 있는 수량을 구한다.

    Args:
        asks: 매도 호가. **가격 오름차순**이어야 한다.
        budget: 쓸 수 있는 금액 (호가와 같은 결제 통화 기준).
    """
    if budget <= 0:
        return WalkResult(0.0, 0.0, 0, False)

    spent = 0.0
    quantity = 0.0

    for level, ask in enumerate(asks, start=1):
        level_cost = ask.price * ask.size
        remaining_budget = budget - spent

        if level_cost >= remaining_budget:
            # 이 단계에서 예산이 끝난다 — 일부만 체결
            quantity += remaining_budget / ask.price
            return WalkResult(quantity, budget, level, False)

        spent += level_cost
        quantity += ask.size

    # 호가를 다 훑었는데도 예산이 남았다
    return WalkResult(quantity, spent, len(asks), True)


def walk_sell(bids: list[OrderBookLevel], quantity: float) -> WalkResult:
    """수량만큼 매수호가를 훑어 받을 수 있는 금액을 구한다.

    Args:
        bids: 매수 호가. **가격 내림차순**이어야 한다.
        quantity: 팔 코인 수량.
    """
    if quantity <= 0:
        return WalkResult(0.0, 0.0, 0, False)

    remaining = quantity
    proceeds = 0.0

    for level, bid in enumerate(bids, start=1):
        if bid.size >= remaining:
            proceeds += remaining * bid.price
            return WalkResult(quantity, proceeds, level, False)

        proceeds += bid.size * bid.price
        remaining -= bid.size

    # 호가를 다 훑었는데도 팔 물량이 남았다
    return WalkResult(quantity - remaining, proceeds, len(bids), True)
