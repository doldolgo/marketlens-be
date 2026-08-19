"""매트릭스 결과 모델.

**모든 코인**에 대해 한 행씩 — 국내가(KRW), 가장 큰 김프 조합, 가장 큰 역프 조합.

김프와 역프는 **서로 다른 거래**이므로 구매처·판매처를 방향마다 따로 둔다.
예: 김프 1등이 (바이낸스 매수 → 업비트 매도) 여도, 역프 1등은
(빗썸 매수 → 바이낸스 매도) 일 수 있다.

데이터는 전부 DB 스냅샷에서 나온다. 거래소 직접 호출은 없다.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class MatrixDirection(BaseModel):
    """한 방향(김프 또는 역프)에서 가장 좋은 거래소 조합의 계산 결과."""

    buy_exchange: str = Field(..., description="구매처 거래소 ID")
    sell_exchange: str = Field(..., description="판매처 거래소 ID")

    premium_percent: float = Field(
        ...,
        description=(
            "**표면 프리미엄** (%). 구매처 최우선 매도호가와 판매처 최우선 "
            "매수호가만 본 값으로, 금액과 무관하다"
        ),
    )
    total_slippage_percent: float = Field(
        ...,
        description=(
            "요청 금액만큼 호가를 실제로 훑었을 때 표면 프리미엄에서 "
            "깎이는 폭 (%p). 실현 수익률 = premium_percent - 이 값"
        ),
    )

    withdrawal_available: bool = Field(
        False,
        description=(
            "**구매처에서 이 코인을 출금할 수 있는지.** 코인을 옮기려면 구매처 "
            "출금이 열려 있어야 한다. 확인 불가(키 없음·API 장애)도 False 다 — "
            "모르는 경로를 열린 것처럼 보여주지 않는다"
        ),
    )
    deposit_available: bool = Field(
        False,
        description=(
            "**판매처에서 이 코인을 입금받을 수 있는지.** 확인 불가도 False."
        ),
    )

    depth_exhausted: bool = Field(
        False,
        description=(
            "저장된 호가가 부족해 요청 금액을 다 채우지 못했는지. "
            "True 면 슬리피지는 요청 금액이 아니라 **실제 체결 가능한 물량 "
            "기준**으로 계산된 값이다 (단위당 손익). "
            "그 물량을 넘는 부분은 이 조합으로는 거래할 수 없다"
        ),
    )


class MatrixCoinEntry(BaseModel):
    """코인 하나의 매트릭스 행."""

    sym: str = Field(..., description="코인 심볼")

    fwd: MatrixDirection | None = Field(
        None,
        description=(
            "**가장 큰 김프** 조합 (해외 매수 → 국내 매도). "
            "계산 가능한 조합이 없으면 null"
        ),
    )
    rev: MatrixDirection | None = Field(
        None,
        description=(
            "**가장 큰 역프** 조합 (국내 매수 → 해외 매도). "
            "계산 가능한 조합이 없으면 null"
        ),
    )

    suspicious: bool = Field(
        False,
        description=(
            "표면 프리미엄이 비정상적으로 커서 티커 충돌(동명이인 코인)이나 "
            "입출금 중단이 의심되는지"
        ),
    )


class MatrixResult(BaseModel):
    """GET /matrix 응답."""

    amount_krw: float = Field(..., description="슬리피지 계산에 쓴 투입 금액 (원화)")

    coins: list[MatrixCoinEntry] = Field(
        default_factory=list,
        description="코인별 결과. 김프 표면 프리미엄 내림차순 정렬",
    )
    scanned_coins: int = Field(..., description="비교된 코인 수")
    scanned_combinations: int = Field(
        ..., description="비교한 (코인 × 국내 × 해외) 조합 수"
    )

    dom_list: list[str] = Field(
        default_factory=list, description="비교에 참여한 국내 거래소"
    )
    fx_list: list[str] = Field(
        default_factory=list, description="비교에 참여한 해외 거래소"
    )

    data_oldest_at: int | None = Field(
        None,
        description=(
            "사용한 스냅샷 중 **가장 오래된** 갱신 시각 (epoch ms). "
            "지금과의 차이가 크면 POST /refresh 로 갱신할 것"
        ),
    )
    data_newest_at: int | None = Field(
        None, description="사용한 스냅샷 중 가장 최근 갱신 시각 (epoch ms)"
    )

    warnings: list[str] = Field(default_factory=list, description="주의 사항")
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
