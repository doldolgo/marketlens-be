"""김치 프리미엄 계산 결과 모델.

프리미엄 정의
    premium_ratio = KRW 가격 / USDT 가격 / 환율

    KRW 가격  : 원화 기준 거래소(업비트)의 해당 코인 가격
    USDT 가격 : 비교 대상 거래소의 해당 코인 가격
    환율      : 업비트 KRW-USDT 중간가

세 값이 완벽히 일치하면 비율은 1.0 이 되고, 프리미엄은 0% 다.
1.0 보다 크면 국내가 비싼 것(김프), 작으면 국내가 싼 것(역프)이다.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.models.orderbook import MarketType
from app.models.ticker import PriceBasis


class PremiumEntry(BaseModel):
    """비교 대상 거래소 한 곳의 프리미엄."""

    exchange: str = Field(..., description="비교 대상 거래소 ID")
    name: str = Field(..., description="거래소 이름")
    symbol: str = Field(..., description="통일 심볼 (예: BTC/USDT)")
    native_symbol: str = Field(..., description="거래소 원본 심볼 (예: BTCUSDT)")
    market_type: MarketType = Field(..., description="현물/선물 구분")

    quote_currency: str = Field(..., description="결제 통화 (예: USDT)")
    price: float = Field(..., description="해당 거래소 가격 (결제 통화 기준, price_basis 적용)")
    price_in_krw: float = Field(..., description="위 가격을 환율로 환산한 원화 값")

    premium_ratio: float = Field(
        ..., description="KRW 가격 / 해외 가격 / 환율. 1.0 이면 프리미엄 없음"
    )
    premium_percent: float = Field(
        ..., description="프리미엄 (%). 양수면 국내가 비쌈(김프), 음수면 국내가 쌈(역프)"
    )
    premium_krw: float = Field(..., description="원화 기준 절대 가격차 (KRW 가격 - 환산 가격)")

    timestamp: int = Field(..., description="거래소 기준 호가 시각 (epoch ms)")
    latency_ms: float = Field(..., description="해당 거래소 호출 지연시간 (ms)")


class PremiumFailure(BaseModel):
    """프리미엄을 계산하지 못한 거래소."""

    exchange: str = Field(..., description="거래소 ID")
    symbol: str = Field(..., description="요청한 통일 심볼")
    error_code: str = Field(..., description="에러 코드")
    message: str = Field(..., description="에러 메시지")


class PremiumResult(BaseModel):
    """프리미엄 조회 응답."""

    base: str = Field(..., description="조회한 코인 (예: BTC)")
    price_basis: PriceBasis = Field(
        ..., description="가격 기준. last=마지막 체결가, mid=호가 중간가"
    )

    krw_exchange: str = Field(..., description="원화 기준 거래소 ID")
    krw_symbol: str = Field(..., description="원화 기준 통일 심볼 (예: BTC/KRW)")
    krw_native_symbol: str = Field(..., description="원화 기준 거래소 원본 심볼 (예: KRW-BTC)")
    krw_price: float = Field(..., description="원화 기준 가격 (price_basis 적용)")
    krw_timestamp: int = Field(..., description="원화 가격 기준 시각 (epoch ms)")

    usdt_krw_rate: float = Field(..., description="적용한 USDT/KRW 환율")
    fx_source: str = Field(..., description="환율 출처")

    premiums: list[PremiumEntry] = Field(
        default_factory=list, description="거래소별 프리미엄. 프리미엄 내림차순 정렬"
    )
    failures: list[PremiumFailure] = Field(
        default_factory=list, description="조회 실패한 거래소 (부분 실패 허용)"
    )

    fetched_at: int = Field(..., description="서버 응답 생성 시각 (epoch ms)")
    elapsed_ms: float = Field(..., description="전체 처리 시간 (ms)")
