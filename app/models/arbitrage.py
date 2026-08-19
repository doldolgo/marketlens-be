"""금액 기준 차익거래 시뮬레이션 결과 모델.

`/premium` 이 "지금 가격차가 몇 %인가"를 알려준다면, 이 모델은
**"실제로 N원을 넣으면 얼마가 남는가"** 를 알려준다.

둘은 다르다. 프리미엄은 최우선 호가(또는 체결가) **한 점**만 보지만,
실제 주문은 호가창을 위에서부터 **훑어 내려가며** 체결된다. 금액이 커질수록
불리한 가격까지 먹게 되고(슬리피지), 프리미엄이 3%여도 실수령은 그보다 적다.

데이터는 전부 ``POST /refresh`` 가 저장해둔 DB 스냅샷에서 나온다.
거래소 직접 호출은 없다.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.models.premium import PremiumDirection


class VenueQuote(BaseModel):
    """비교 후보에 오른 거래소 한 곳의 최우선 시세 (원화 환산)."""

    exchange: str = Field(..., description="거래소 ID")
    name: str = Field(..., description="거래소 이름")

    best_bid_krw: float = Field(
        ..., description="최우선 매수호가 (원화 환산) — 여기 팔면 받는 값"
    )
    best_ask_krw: float = Field(
        ..., description="최우선 매도호가 (원화 환산) — 여기 사면 내는 값"
    )
    depth_levels: int = Field(..., description="확보한 호가 단계 수")


class ExecutionSide(BaseModel):
    """한쪽 체결(매수 또는 매도) 시뮬레이션 결과."""

    exchange: str = Field(..., description="거래소 ID")
    name: str = Field(..., description="거래소 이름")

    average_price_krw: float = Field(
        ..., description="실제 평균 체결가 (원화 환산)"
    )
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

    data_updated_at: int | None = Field(
        None, description="이 호가 스냅샷을 DB 에 저장한 시각 (epoch ms)"
    )


class ArbitrageFailure(BaseModel):
    """조회에 실패한 거래소."""

    exchange: str = Field(..., description="거래소 ID")
    symbol: str = Field(..., description="요청한 통일 심볼")
    error_code: str = Field(..., description="에러 코드")
    message: str = Field(..., description="에러 메시지")


class ArbitrageResult(BaseModel):
    """금액 기준 차익거래 시뮬레이션 응답."""

    sym: str = Field(..., description="대상 코인 심볼")
    direction: PremiumDirection | None = Field(
        None,
        description=(
            "고정한 차익 방향. `fwd`=해외 매수→국내 매도, `rev`=국내 매수→해외 매도. "
            "`null` 이면 자동 선택 — 가장 싼 곳에서 사고 가장 비싼 곳에서 판다. "
            "자동 선택은 **가능한 조합 중 가장 유리한 것**일 뿐이며, 스프레드가 "
            "가격차보다 크면 음수 수익이 나올 수 있다 (`warnings` 참고)"
        ),
    )

    input_amount_krw: float = Field(
        ..., description="투입 금액의 원화 환산 (기준 환율 적용)"
    )

    usd_krw_rate: float = Field(
        ...,
        description=(
            "적용한 통일 환율 — 하나은행 고시 USD/KRW 매매기준율 (DB `usdkrw_rate`). "
            "해외 USDT 가격에 이 값을 곱해 원화 환산한다 (USDT≈USD 페그 전제)"
        ),
    )

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

    withdrawal_available: bool | None = Field(
        None,
        description=(
            "**매수처에서 이 코인을 출금할 수 있는지.** 코인을 매도처로 옮겨야 "
            "차익이 실현되므로 False 면 이 경로는 실행 불가능하다. "
            "`true`=확인했고 열림 / `false`=확인했고 막힘 / "
            "`null`=**확인 불가**(키 없음·API 장애·응답 누락). "
            "**null 을 열림으로 읽지 말 것**"
        ),
    )
    deposit_available: bool | None = Field(
        None,
        description=(
            "**매도처에서 이 코인을 입금받을 수 있는지.** False 면 이 경로는 "
            "실행 불가능하다. 값의 뜻은 `withdrawal_available` 과 같다 "
            "(`null`=확인 불가)."
        ),
    )

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

    data_oldest_at: int | None = Field(
        None,
        description=(
            "비교에 쓴 스냅샷 중 **가장 오래된** 갱신 시각 (epoch ms). "
            "지금과의 차이가 크면 POST /refresh 로 갱신할 것"
        ),
    )
    data_newest_at: int | None = Field(
        None, description="비교에 쓴 스냅샷 중 가장 최근 갱신 시각 (epoch ms)"
    )

    data_received_at: int | None = Field(
        None,
        description=(
            "이 응답의 데이터를 **거래소에서 받은** 시각 (epoch ms). "
            "응답을 만든 시각(fetched_at) · 코인별 스냅샷 갱신 시각"
            "(data_updated_at)과 뜻이 다르다"
        ),
    )
    fetched_at: int = Field(..., description="서버 응답 생성 시각 (epoch ms)")
    elapsed_ms: float = Field(..., description="전체 처리 시간 (ms)")
