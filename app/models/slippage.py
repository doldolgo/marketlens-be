"""슬리피지 조회 결과 모델.

슬리피지는 **최우선 호가와 실제 평균 체결가의 차이**다.

한 호가 단계에는 정해진 잔량만 있어서, 그보다 많이 거래하면 다음 단계로 파고들며
가격이 불리해진다. 이 모델은 그 과정을 단계별로 보여준다.

    슬리피지(%) = (평균 체결가 - 최우선 호가) / 최우선 호가 × 100

매도는 부호를 뒤집어 **어느 방향이든 "나에게 불리해진 정도"** 가 되게 한다.
따라서 항상 0 이상이다.

데이터는 전부 DB 에 저장된 호가 스냅샷에서 나온다. 거래소 직접 호출은 없다.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class OrderSide(str, Enum):
    """주문 방향."""

    #: 매수 — 매도호가(asks)를 위에서부터 훑는다
    BUY = "buy"
    #: 매도 — 매수호가(bids)를 위에서부터 훑는다
    SELL = "sell"


class FillLevel(BaseModel):
    """한 호가 단계에서 체결된 몫."""

    level: int = Field(..., description="호가 단계 (1 = 최우선)")
    price: float = Field(..., description="그 단계의 호가")
    size: float = Field(
        ..., description="그 단계에서 체결된 수량. 마지막 단계는 잔량보다 작을 수 있다"
    )
    amount: float = Field(..., description="그 단계에서 오간 금액 (price × size)")
    cumulative_quantity: float = Field(..., description="여기까지 누적 수량")
    cumulative_amount: float = Field(..., description="여기까지 누적 금액")
    cumulative_average: float = Field(
        ..., description="여기까지의 평균 체결가 (= 누적금액 / 누적수량)"
    )


class SlippageResult(BaseModel):
    """슬리피지 계산 결과."""

    exchange: str = Field(..., description="거래소 ID")
    name: str = Field(..., description="거래소 이름")
    symbol: str = Field(..., description="통일 심볼")
    quote_currency: str = Field(..., description="결제 통화")

    side: OrderSide = Field(..., description="매수/매도")
    requested_amount: float | None = Field(
        None, description="요청한 금액 (금액 기준으로 조회한 경우)"
    )
    requested_quantity: float | None = Field(
        None, description="요청한 수량 (수량 기준으로 조회한 경우)"
    )

    best_price: float = Field(
        ..., description="최우선 호가. 매수면 최저 매도호가, 매도면 최고 매수호가"
    )
    average_price: float = Field(..., description="**실제 평균 체결가**")
    worst_price: float = Field(..., description="마지막으로 체결된 단계의 호가")

    quantity: float = Field(..., description="체결된 코인 수량")
    amount: float = Field(..., description="오간 금액 (매수=지출, 매도=수령)")

    slippage_percent: float = Field(
        ...,
        description=(
            "최우선 호가 대비 얼마나 불리해졌는지 (%). "
            "매수·매도 모두 **항상 0 이상**이며 클수록 손해"
        ),
    )
    slippage_cost: float = Field(
        ...,
        description=(
            "슬리피지로 인한 손해액 (결제 통화). "
            "최우선 호가로 전부 체결됐다면 얻었을 결과와의 차이"
        ),
    )

    levels_consumed: int = Field(..., description="소진한 호가 단계 수")
    depth_exhausted: bool = Field(
        ..., description="호가창이 부족해 요청을 다 채우지 못했는지"
    )
    depth_available: int = Field(..., description="확보한 호가 단계 수")
    top_level_amount: float = Field(
        ..., description="최우선 호가 1단계에서 체결 가능한 금액. 이 이하면 슬리피지 0"
    )

    fills: list[FillLevel] = Field(
        default_factory=list,
        description="단계별 체결 내역 (업비트 호가창 툴팁과 같은 값)",
    )

    data_updated_at: int | None = Field(
        None,
        description=(
            "이 계산에 쓴 스냅샷의 DB 갱신 시각 (epoch ms). "
            "지금과의 차이가 크면 POST /refresh 로 갱신할 것"
        ),
    )
    data_received_at: int | None = Field(
        None,
        description=(
            "이 응답의 데이터를 **거래소에서 받은** 시각 (epoch ms). "
            "코인별 스냅샷 갱신 시각(data_updated_at)과 뜻이 다르다"
        ),
    )

    warnings: list[str] = Field(default_factory=list, description="주의 사항")
