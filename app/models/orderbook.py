"""거래소 호가(orderbook) 도메인 모델.

거래소마다 응답 스키마가 전부 다르기 때문에, 각 Exchange 구현체가
자신의 원본 응답을 여기 정의된 통일 모델로 변환(normalize)한다.
API 레이어와 비교 로직은 원본 스키마를 절대 알 필요가 없다.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class MarketType(str, Enum):
    """거래 시장 구분."""

    SPOT = "spot"
    FUTURES = "futures"


class OrderBookLevel(BaseModel):
    """호가 한 단계 (가격 + 잔량)."""

    price: float = Field(..., description="호가 가격 (해당 마켓의 quote 통화 기준)")
    size: float = Field(..., description="해당 가격의 잔량 (base 통화 기준)")


class OrderBook(BaseModel):
    """통일된 호가창 모델."""

    exchange: str = Field(..., description="거래소 ID (예: upbit, binance)")
    symbol: str = Field(..., description="통일 심볼 (예: BTC/KRW, BTC/USDT)")
    native_symbol: str = Field(..., description="거래소 원본 심볼 (예: KRW-BTC, BTCUSDT)")
    market_type: MarketType = Field(default=MarketType.SPOT, description="현물/선물 구분")
    base: str = Field(..., description="기준 통화 (예: BTC)")
    quote: str = Field(..., description="결제 통화 (예: KRW, USDT)")

    bids: list[OrderBookLevel] = Field(default_factory=list, description="매수 호가, 가격 내림차순")
    asks: list[OrderBookLevel] = Field(default_factory=list, description="매도 호가, 가격 오름차순")

    timestamp: int = Field(..., description="거래소 기준 호가 시각 (epoch milliseconds)")
    latency_ms: float = Field(..., description="요청 시작~응답 파싱 완료까지 걸린 시간 (ms)")

    @property
    def best_bid(self) -> float | None:
        """최우선 매수호가 (내가 시장가로 팔 때 체결되는 가격)."""
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        """최우선 매도호가 (내가 시장가로 살 때 체결되는 가격)."""
        return self.asks[0].price if self.asks else None

    @property
    def mid_price(self) -> float | None:
        """best_bid 와 best_ask 의 중간값."""
        bid, ask = self.best_bid, self.best_ask
        if bid is None or ask is None:
            return None
        return (bid + ask) / 2

    @property
    def spread(self) -> float | None:
        """호가 스프레드 (ask - bid)."""
        bid, ask = self.best_bid, self.best_ask
        if bid is None or ask is None:
            return None
        return ask - bid
