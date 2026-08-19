"""거래소 간 가격 비교 결과 모델.

데이터는 전부 DB 스냅샷(``market_snapshots`` / ``usdkrw_rate``)에서 나온다.
거래소 직접 호출은 없다.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ExchangeQuote(BaseModel):
    """비교에 참여한 거래소 한 곳의 요약 시세."""

    exchange: str = Field(..., description="거래소 ID")

    quote_currency: str = Field(..., description="원래 결제 통화 (KRW, USDT)")
    price: float = Field(
        ..., description="현재 가격 — 마지막 체결가 (공통 통화 환산)"
    )
    best_bid: float | None = Field(
        None,
        description="최우선 매수호가 (공통 통화 환산). 저장된 호가가 없으면 null",
    )
    best_ask: float | None = Field(
        None,
        description="최우선 매도호가 (공통 통화 환산). 저장된 호가가 없으면 null",
    )

    data_updated_at: int | None = Field(
        None,
        description=(
            "이 스냅샷을 DB 에 저장한 시각 (epoch ms). "
            "지금과의 차이가 크면 POST /refresh 로 갱신할 것"
        ),
    )


class ArbitrageSpread(BaseModel):
    """가장 싸게 살 수 있는 곳과 가장 비싸게 팔 수 있는 곳의 차이."""

    buy_exchange: str = Field(
        ..., description="가장 싸게 매수 가능한 거래소 (best ask 최저)"
    )
    buy_price: float = Field(..., description="해당 거래소 매수 가격 (공통 통화)")
    sell_exchange: str = Field(
        ..., description="가장 비싸게 매도 가능한 거래소 (best bid 최고)"
    )
    sell_price: float = Field(..., description="해당 거래소 매도 가격 (공통 통화)")

    absolute: float = Field(..., description="sell_price - buy_price (공통 통화)")
    percent: float = Field(..., description="buy_price 대비 수익률 (%). 수수료 미반영")


class ComparisonResult(BaseModel):
    """거래소 간 가격 비교 응답."""

    sym: str = Field(..., description="비교 대상 코인 심볼 (예: BTC)")
    common_currency: str = Field(..., description="비교 기준 통화 (환산 기준)")
    usd_krw_rate: float | None = Field(
        None,
        description=(
            "적용한 통일 환율 — 하나은행 고시 USD/KRW 매매기준율 (DB `usdkrw_rate`). "
            "아직 수집 전이면 null"
        ),
    )

    quotes: list[ExchangeQuote] = Field(
        default_factory=list,
        description="거래소별 시세 (공통 통화 환산). price 오름차순 정렬",
    )
    missing_exchanges: list[str] = Field(
        default_factory=list,
        description="요청한 거래소 중 이 코인의 스냅샷이 DB 에 없는 곳",
    )

    spread: ArbitrageSpread | None = Field(
        None,
        description=(
            "최저 매수처 / 최고 매도처 차이. "
            "호가가 저장된 거래소가 2곳 미만이면 null"
        ),
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

    data_received_at: int | None = Field(
        None,
        description=(
            "이 응답의 데이터를 **거래소에서 받은** 시각 (epoch ms). "
            "응답을 만든 시각(fetched_at) · 코인별 스냅샷 갱신 시각"
            "(data_updated_at)과 뜻이 다르다"
        ),
    )
    fetched_at: int = Field(..., description="서버 응답 생성 시각 (epoch ms)")
    elapsed_ms: float = Field(..., description="전체 비교 처리에 걸린 시간 (ms)")
