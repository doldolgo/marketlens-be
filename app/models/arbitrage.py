"""금액 기준 차익거래 시뮬레이션 결과 모델.

`/premium` 이 "지금 가격차가 몇 %인가"를 알려준다면, 이 모델은
**"실제로 N원을 넣으면 얼마가 남는가"** 를 알려준다.

둘은 다르다. 프리미엄은 최우선 호가(또는 체결가) **한 점**만 보지만,
실제 주문은 호가창을 위에서부터 **훑어 내려가며** 체결된다. 금액이 커질수록
불리한 가격까지 먹게 되고(슬리피지), 프리미엄이 3%여도 실수령은 그보다 적다.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.models.orderbook import MarketType


class VenueQuote(BaseModel):
    """비교 후보에 오른 거래소 한 곳의 최우선 시세 (원화 환산)."""

    exchange: str = Field(..., description="거래소 ID")
    name: str = Field(..., description="거래소 이름")
    symbol: str = Field(..., description="통일 심볼")
    native_symbol: str = Field(..., description="거래소 원본 심볼")
    quote_currency: str = Field(..., description="결제 통화")

    best_bid_krw: float = Field(..., description="최우선 매수호가 (원화 환산) — 여기 팔면 받는 값")
    best_ask_krw: float = Field(..., description="최우선 매도호가 (원화 환산) — 여기 사면 내는 값")
    mid_price_krw: float = Field(..., description="중간가 (원화 환산)")
    depth_levels: int = Field(..., description="확보한 호가 단계 수")


class ExecutionSide(BaseModel):
    """한쪽 체결(매수 또는 매도) 시뮬레이션 결과."""

    exchange: str = Field(..., description="거래소 ID")
    name: str = Field(..., description="거래소 이름")
    symbol: str = Field(..., description="통일 심볼")
    native_symbol: str = Field(..., description="거래소 원본 심볼")
    quote_currency: str = Field(..., description="결제 통화 (KRW / USDT)")

    best_price: float = Field(..., description="최우선 호가 (결제 통화 기준)")
    average_price: float = Field(..., description="실제 평균 체결가 (결제 통화 기준)")
    average_price_krw: float = Field(..., description="평균 체결가 원화 환산")

    amount: float = Field(..., description="소요/수령 금액 (결제 통화 기준)")
    amount_krw: float = Field(..., description="소요/수령 금액 (원화 환산)")

    slippage_percent: float = Field(
        ...,
        description=(
            "최우선 호가 대비 평균 체결가가 얼마나 불리해졌는지 (%). "
            "항상 0 이상이며, 클수록 호가가 얕다는 뜻"
        ),
    )
    levels_consumed: int = Field(..., description="소진한 호가 단계 수")
    depth_exhausted: bool = Field(
        ..., description="호가창이 부족해 요청 수량을 다 채우지 못했는지"
    )

    timestamp: int = Field(..., description="호가 기준 시각 (epoch ms)")
    latency_ms: float = Field(..., description="해당 거래소 호출 지연시간 (ms)")


class ArbitrageFailure(BaseModel):
    """조회에 실패한 거래소."""

    exchange: str = Field(..., description="거래소 ID")
    symbol: str = Field(..., description="요청한 통일 심볼")
    error_code: str = Field(..., description="에러 코드")
    message: str = Field(..., description="에러 메시지")


class ArbitrageResult(BaseModel):
    """금액 기준 차익거래 시뮬레이션 응답."""

    base: str = Field(..., description="대상 코인")
    market_type: MarketType = Field(..., description="현물/선물 구분")

    input_amount: float = Field(..., description="입력한 투입 금액")
    input_currency: str = Field(..., description="입력 금액의 통화 (KRW / USDT)")
    input_amount_krw: float = Field(..., description="투입 금액의 원화 환산")

    usdt_krw_rate: float = Field(..., description="적용한 USDT/KRW 환율")
    fx_source: str = Field(..., description="환율 출처")

    premium_percent: float = Field(
        ...,
        description=(
            "최우선 호가 기준 프리미엄 (%). 비싼 곳이 싼 곳보다 몇 % 높은지. "
            "슬리피지를 반영하지 않은 '표면상' 가격차"
        ),
    )

    buy: ExecutionSide = Field(..., description="싼 곳에서의 매수 시뮬레이션")
    sell: ExecutionSide = Field(..., description="비싼 곳에서의 매도 시뮬레이션")

    quantity: float = Field(..., description="싼 곳에서 매수된 코인 개수")

    profit_krw: float = Field(..., description="차익 (원화). 매도 수령액 - 매수 소요액")
    profit_percent: float = Field(
        ..., description="투입 금액 대비 수익률 (%). **슬리피지 반영, 수수료 미반영**"
    )
    premium_capture_percent: float = Field(
        ...,
        description=(
            "표면 프리미엄 중 실제로 실현된 비율 (%). "
            "100 이면 슬리피지 없음, 낮을수록 호가가 얕아 손실이 큼"
        ),
    )

    candidates: list[VenueQuote] = Field(
        default_factory=list, description="비교 대상 거래소들의 최우선 시세 (싼 곳부터)"
    )
    failures: list[ArbitrageFailure] = Field(
        default_factory=list, description="조회에 실패한 거래소"
    )
    warnings: list[str] = Field(
        default_factory=list, description="결과 해석 시 반드시 확인해야 할 경고"
    )

    fetched_at: int = Field(..., description="서버 응답 생성 시각 (epoch ms)")
    elapsed_ms: float = Field(..., description="전체 처리 시간 (ms)")
