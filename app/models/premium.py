"""김치 프리미엄 / 역프리미엄 계산 결과 모델.

두 방향은 **서로 다른 거래**다. 단순히 부호만 뒤집은 값이 아니다.

    김프   (fwd)  : 해외에서 사와서 → 국내에 판다
    역김프 (rev) : 국내에서 사서   → 해외에 판다

각 방향의 수익률은 그 방향으로 거래했을 때 코인 1개당 몇 % 남는지다.
**양수면 그 방향이 이득, 음수면 손해**다. 두 방향이 동시에 음수일 수 있고,
스프레드가 넓은 종목에서는 그게 정상이다.

가격은 항상 **실제로 체결되는 쪽 호가**를 쓰므로 방향마다 쓰는 호가가 다르다.

    김프   : 해외 매도호가(ask)로 사서 → 국내 매수호가(bid)에 판다
    역김프 : 국내 매도호가(ask)로 사서 → 해외 매수호가(bid)에 판다

따라서 두 값은 완전히 독립적이다.

데이터는 전부 DB 스냅샷에서 나온다. 거래소 직접 호출은 없다.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class PremiumDirection(str, Enum):
    """차익 방향."""

    #: 김프 — 해외에서 매수해 국내에 매도. 국내가 비쌀 때 이득.
    FWD = "fwd"
    #: 역김프 — 국내에서 매수해 해외에 매도. 해외가 비쌀 때 이득.
    REV = "rev"


class PremiumEntry(BaseModel):
    """해외 거래소 한 곳과의 프리미엄."""

    fx: str = Field(..., description="해외 거래소 ID")
    fx_name: str = Field(..., description="해외 거래소 이름")

    usd: float = Field(
        ...,
        description=(
            "해외 가격 (USDT). 김프면 매도호가(ask), 역김프면 매수호가(bid) 기준. "
            "원화 환산은 usd_krw_rate 를 곱하면 된다"
        ),
    )

    premium_percent: float = Field(
        ...,
        description=(
            "이 방향으로 거래했을 때 수익률 (%). "
            "**양수면 이 방향이 이득, 음수면 손해.** 수수료 미반영"
        ),
    )
    premium_krw: float = Field(
        ..., description="코인 1개당 원화 차익 (매도측 - 매수측)"
    )
    profitable: bool = Field(
        ..., description="이 방향으로 이득인지 (premium_percent > 0)"
    )

    data_updated_at: int | None = Field(
        None, description="이 해외 스냅샷이 DB 에 저장된 시각 (epoch ms)"
    )


class PremiumFailure(BaseModel):
    """프리미엄을 계산하지 못한 거래소."""

    exchange: str = Field(..., description="거래소 ID")
    symbol: str = Field(..., description="요청한 통일 심볼")
    error_code: str = Field(..., description="에러 코드")
    message: str = Field(..., description="에러 메시지")


class PremiumResult(BaseModel):
    """프리미엄 조회 응답."""

    sym: str = Field(..., description="조회한 코인 심볼 (예: BTC)")
    direction: PremiumDirection = Field(
        ...,
        description="차익 방향. fwd=해외 매수→국내 매도, rev=국내 매수→해외 매도",
    )

    dom: str = Field(..., description="국내 거래소 ID")
    dom_price: float = Field(
        ...,
        description=(
            "국내 가격 (KRW). 김프면 매수호가(bid), 역김프면 매도호가(ask) 기준"
        ),
    )

    usd_krw_rate: float = Field(
        ...,
        description=(
            "적용한 통일 환율 — 하나은행 고시 USD/KRW 매매기준율 (DB `fx_rate`). "
            "해외 USDT 가격에 이 값을 곱해 원화 환산한다 (USDT≈USD 페그 전제)"
        ),
    )
    rate_updated_at: int | None = Field(
        None, description="환율이 DB 에 저장된 시각 (epoch ms)"
    )

    premiums: list[PremiumEntry] = Field(
        default_factory=list, description="해외 거래소별 프리미엄. 수익률 내림차순 정렬"
    )
    failures: list[PremiumFailure] = Field(
        default_factory=list,
        description="스냅샷이 없거나 가격을 뽑지 못한 거래소 (부분 실패 허용)",
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

    fetched_at: int = Field(..., description="서버 응답 생성 시각 (epoch ms)")
    elapsed_ms: float = Field(..., description="전체 처리 시간 (ms)")
