"""거래소 간 가격 비교 결과 모델."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.models.orderbook import MarketType


class ExchangeQuote(BaseModel):
    """비교에 참여한 거래소 한 곳의 요약 시세."""

    exchange: str = Field(..., description="거래소 ID")
    symbol: str = Field(..., description="통일 심볼")
    native_symbol: str = Field(..., description="거래소 원본 심볼")
    market_type: MarketType = Field(..., description="현물/선물 구분")

    quote_currency: str = Field(..., description="원래 결제 통화 (KRW, USDT 등)")
    best_bid: float = Field(..., description="최우선 매수호가 (원래 통화)")
    best_ask: float = Field(..., description="최우선 매도호가 (원래 통화)")
    mid_price: float = Field(..., description="중간가 (원래 통화)")

    # 비교를 위해 공통 통화로 환산한 값
    best_bid_converted: float = Field(..., description="최우선 매수호가 (공통 통화 환산)")
    best_ask_converted: float = Field(..., description="최우선 매도호가 (공통 통화 환산)")
    mid_price_converted: float = Field(..., description="중간가 (공통 통화 환산)")

    timestamp: int = Field(..., description="거래소 기준 호가 시각 (epoch ms)")
    latency_ms: float = Field(..., description="해당 거래소 호출 지연시간 (ms)")


class ExchangeFailure(BaseModel):
    """조회에 실패한 거래소."""

    exchange: str = Field(..., description="거래소 ID")
    symbol: str = Field(..., description="요청한 통일 심볼")
    error_code: str = Field(..., description="에러 코드")
    message: str = Field(..., description="에러 메시지")


class ArbitrageSpread(BaseModel):
    """가장 싸게 살 수 있는 곳과 가장 비싸게 팔 수 있는 곳의 차이."""

    buy_exchange: str = Field(..., description="가장 싸게 매수 가능한 거래소 (best ask 최저)")
    buy_price: float = Field(..., description="해당 거래소 매수 가격 (공통 통화)")
    sell_exchange: str = Field(..., description="가장 비싸게 매도 가능한 거래소 (best bid 최고)")
    sell_price: float = Field(..., description="해당 거래소 매도 가격 (공통 통화)")

    absolute: float = Field(..., description="sell_price - buy_price (공통 통화)")
    percent: float = Field(..., description="buy_price 대비 수익률 (%). 수수료 미반영")


class ComparisonResult(BaseModel):
    """거래소 간 가격 비교 응답."""

    base: str = Field(..., description="비교 대상 코인 (예: BTC)")
    common_currency: str = Field(..., description="비교 기준 통화 (환산 기준)")
    usdt_krw_rate: float = Field(..., description="환산에 사용한 USDT/KRW 환율")
    fx_source: str = Field(..., description="환율 산출 출처")

    quotes: list[ExchangeQuote] = Field(default_factory=list, description="거래소별 시세")
    failures: list[ExchangeFailure] = Field(
        default_factory=list, description="조회에 실패한 거래소 (부분 실패 허용)"
    )

    spread: ArbitrageSpread | None = Field(
        None, description="최저 매수처 / 최고 매도처 차이. 비교 가능한 거래소가 2곳 미만이면 null"
    )

    fetched_at: int = Field(..., description="서버 응답 생성 시각 (epoch ms)")
    elapsed_ms: float = Field(..., description="전체 비교 처리에 걸린 시간 (ms)")
