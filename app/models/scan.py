"""전종목 프리미엄 스캔 결과 모델.

`/premium/fwd` 와 `/premium/rev` 가 **코인 하나**를 보는 것이라면,
스캔은 **국내에 상장된 모든 코인**을 훑어 두 방향 각각의 1등을 찾아낸다.

데이터는 전부 DB 스냅샷에서 나온다. 거래소 직접 호출은 없다.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from app.models.premium import PremiumDirection


class SortOrder(str, Enum):
    """목록 정렬 방향."""

    #: 수익률 오름차순 (기본) — 낮은 값부터
    ASC = "asc"
    #: 수익률 내림차순 — 높은 값부터
    DESC = "desc"


class ScanEntry(BaseModel):
    """코인 하나 × 거래소 조합의 스캔 결과."""

    sym: str = Field(..., description="코인 심볼 (예: BTC)")
    direction: PremiumDirection = Field(..., description="차익 방향")

    dom: str = Field(..., description="국내 거래소 ID")
    dom_price: float = Field(
        ...,
        description="국내 가격 (KRW). 김프면 매수호가(bid), 역김프면 매도호가(ask)",
    )

    fx: str = Field(..., description="해외 거래소 ID")
    fx_name: str = Field(..., description="해외 거래소 이름")
    usd: float = Field(
        ...,
        description=(
            "해외 가격 (USDT). 김프면 매도호가(ask), 역김프면 매수호가(bid). "
            "원화 환산은 usd_krw_rate 를 곱하면 된다"
        ),
    )

    premium_percent: float = Field(
        ..., description="이 방향의 수익률 (%). 양수면 이득, 음수면 손해"
    )
    premium_krw: float = Field(..., description="코인 1개당 원화 차익")

    liquidity_krw: float | None = Field(
        None,
        description=(
            "저장된 최우선 호가 기준 체결 가능 금액 (원화, 잔량 × 가격). "
            "매수·매도 양쪽 중 **작은 쪽**"
        ),
    )

    suspicious: bool = Field(
        False,
        description=(
            "**그대로 믿으면 안 되는 값.** 프리미엄이 비정상적으로 크면 표시된다. "
            "대부분 티커 충돌이거나 입출금 중단으로 가격이 따로 노는 경우다"
        ),
    )
    suspicion_reason: str | None = Field(
        None, description="의심 사유. `suspicious` 가 false 면 null"
    )


class ScanResult(BaseModel):
    """전종목 스캔 응답."""

    order: SortOrder = Field(
        SortOrder.ASC,
        description="`top_fwd` / `top_rev` 목록의 정렬 방향. `best_*` 는 항상 최대값",
    )

    dom: str = Field(..., description="국내 기준 거래소 ID")
    fx_list: list[str] = Field(
        default_factory=list, description="비교에 참여한 해외 거래소"
    )

    usd_krw_rate: float = Field(
        ...,
        description=(
            "적용한 통일 환율 — 하나은행 고시 USD/KRW 매매기준율 (DB `usdkrw_rate`). "
            "해외 USDT 가격에 이 값을 곱해 원화 환산한다 (USDT≈USD 페그 전제)"
        ),
    )
    rate_updated_at: int | None = Field(
        None, description="환율이 DB 에 저장된 시각 (epoch ms)"
    )

    scanned_coins: int = Field(..., description="양쪽에 모두 상장되어 비교된 코인 수")
    scanned_pairs: int = Field(..., description="비교한 (코인 × 해외 거래소) 조합 수")
    filtered_out: int = Field(0, description="유동성 필터로 제외된 조합 수")
    excluded_bases: list[str] = Field(
        default_factory=list,
        description="티커 충돌이 확인되어 스캔에서 제외한 코인 (설정 `scan_excluded_bases`)",
    )
    suspicious_count: int = Field(0, description="의심 표시가 붙은 조합 수")

    best_fwd: ScanEntry | None = Field(
        None, description="**김프 수익률 1등** (해외 매수 → 국내 매도). 없으면 null"
    )
    best_rev: ScanEntry | None = Field(
        None, description="**역김프 수익률 1등** (국내 매수 → 해외 매도). 없으면 null"
    )

    top_fwd: list[ScanEntry] = Field(
        default_factory=list, description="김프 목록. `order` 방향으로 정렬"
    )
    top_rev: list[ScanEntry] = Field(
        default_factory=list, description="역김프 목록. `order` 방향으로 정렬"
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

    warnings: list[str] = Field(
        default_factory=list, description="결과 해석 시 반드시 확인해야 할 경고"
    )

    fetched_at: int = Field(..., description="서버 응답 생성 시각 (epoch ms)")
    elapsed_ms: float = Field(..., description="전체 처리 시간 (ms)")
