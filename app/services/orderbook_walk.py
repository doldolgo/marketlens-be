"""호가창 소진(order book walk) 계산.

시장가 주문은 최우선 호가 하나로 전부 체결되지 않는다. 그 단계의 잔량을 다 먹으면
다음 단계로 넘어가고, 그럴수록 가격이 불리해진다. 이 모듈은 그 과정을 그대로
계산해 **평균 체결가**와 **슬리피지**를 구한다.

멈추는 조건이 두 가지라서 함수도 두 개다.

    walk_by_amount(levels, amount)     — 금액을 다 쓸 때까지  → 수량이 나온다
    walk_by_quantity(levels, quantity) — 수량을 다 채울 때까지 → 금액이 나온다

어느 쪽 호가를 넘기느냐로 매수/매도가 결정된다.

    매수 : asks (오름차순) 를 넘긴다
    매도 : bids (내림차순) 를 넘긴다
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.models.orderbook import OrderBookLevel


@dataclass(frozen=True, slots=True)
class Fill:
    """한 호가 단계에서 체결된 몫."""

    #: 그 단계의 호가
    price: float
    #: 그 단계에서 체결된 수량 (호가 잔량보다 작을 수 있다 — 마지막 단계)
    size: float

    @property
    def amount(self) -> float:
        """그 단계에서 오간 금액."""
        return self.price * self.size


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
    #: 단계별 체결 내역
    fills: list[Fill] = field(default_factory=list)

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


def walk_by_amount(levels: list[OrderBookLevel], amount: float) -> WalkResult:
    """**금액**을 다 쓸 때까지 호가를 훑는다.

    Args:
        levels: 훑을 호가. 매수면 asks(오름차순), 매도면 bids(내림차순).
        amount: 쓸(또는 받을) 금액. 호가와 같은 결제 통화 기준.
    """
    if amount <= 0:
        return WalkResult(0.0, 0.0, 0, False)

    spent = 0.0
    quantity = 0.0
    fills: list[Fill] = []

    for level, lv in enumerate(levels, start=1):
        level_amount = lv.price * lv.size
        remaining = amount - spent

        if level_amount >= remaining:
            # 이 단계에서 금액이 끝난다 — 일부만 체결
            taken = remaining / lv.price
            fills.append(Fill(lv.price, taken))
            return WalkResult(quantity + taken, amount, level, False, fills)

        spent += level_amount
        quantity += lv.size
        fills.append(Fill(lv.price, lv.size))

    # 호가를 다 훑었는데도 금액이 남았다
    return WalkResult(quantity, spent, len(levels), True, fills)


def walk_by_quantity(levels: list[OrderBookLevel], quantity: float) -> WalkResult:
    """**수량**을 다 채울 때까지 호가를 훑는다.

    Args:
        levels: 훑을 호가. 매수면 asks(오름차순), 매도면 bids(내림차순).
        quantity: 사거나 팔 코인 수량.
    """
    if quantity <= 0:
        return WalkResult(0.0, 0.0, 0, False)

    remaining = quantity
    total = 0.0
    fills: list[Fill] = []

    for level, lv in enumerate(levels, start=1):
        if lv.size >= remaining:
            total += remaining * lv.price
            fills.append(Fill(lv.price, remaining))
            return WalkResult(quantity, total, level, False, fills)

        total += lv.size * lv.price
        remaining -= lv.size
        fills.append(Fill(lv.price, lv.size))

    # 호가를 다 훑었는데도 채우지 못했다
    return WalkResult(quantity - remaining, total, len(levels), True, fills)


#: 매수는 예산으로 asks 를 훑는다.
walk_buy = walk_by_amount
#: 매도는 수량으로 bids 를 훑는다.
walk_sell = walk_by_quantity


def market_price(
    bids: list[OrderBookLevel],
    asks: list[OrderBookLevel],
    amount: float,
) -> float | None:
    """**시장가** — 그 금액이 체결을 끝내는 지점을 양쪽에서 잡아 평균한다.

    누군가 그 금액만큼 거래하면 호가를 훑다가 **어느 단계에서 체결이 끝난다.**
    그 종료 지점의 가격은 매수 쪽에도 매도 쪽에도 하나씩 있고, 둘의 평균이
    "그 규모에서의 시장가" 다.

        시장가 = (asks 를 amount 만큼 훑고 끝난 지점의 호가
                 + bids 를 amount 만큼 훑고 끝난 지점의 호가) / 2

    금액이 커질수록 양쪽 종료 지점이 바깥으로 밀려 값이 벌어진다.
    금액이 최우선 호가 1단계 안이면 ``(최우선 매도호가 + 최우선 매수호가) / 2`` 와
    같아진다 — 즉 **중간가는 이 정의의 특수한 경우**다.

    Args:
        bids: 매수 호가 (가격 내림차순).
        asks: 매도 호가 (가격 오름차순).
        amount: 기준 금액. 호가와 같은 결제 통화.

    Returns:
        시장가. 어느 한쪽이라도 체결이 불가능하거나, **호가가 소진되어 그 금액이
        체결을 끝내는 지점이 존재하지 않으면** ``None``. 소진을 무시하고 잘린
        호가의 마지막 단계를 반환하면 시장 충격이 과소평가된다.
    """
    if amount <= 0 or not bids or not asks:
        return None

    ask_walk = walk_by_amount(asks, amount)
    bid_walk = walk_by_amount(bids, amount)
    if not ask_walk.fills or not bid_walk.fills:
        return None
    if ask_walk.exhausted or bid_walk.exhausted:
        return None

    # 각 방향에서 마지막으로 체결된 단계의 호가
    return (ask_walk.fills[-1].price + bid_walk.fills[-1].price) / 2
