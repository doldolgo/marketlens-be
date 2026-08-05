"""티커(현재가) 도메인 모델.

호가창이 "지금 거래 가능한 가격"이라면, 티커는 **"마지막으로 실제 체결된 가격"** 이다.
흔히 말하는 '현재가'가 이 값이며, 프리미엄 계산의 기본 기준이다.

시가·고가·저가·등락률·거래량 같은 기간 요약은 **의도적으로 담지 않는다.**
거래소마다 집계 구간이 달라서(바이낸스는 롤링 24시간, 업비트는 00:00 UTC 기준 당일)
같은 필드에 다른 의미가 섞이기 때문이다. 필요해지면 구간을 직접 계산해서 넣는다.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from app.models.orderbook import MarketType


class PriceBasis(str, Enum):
    """가격 비교의 기준을 무엇으로 삼을지."""

    #: 마지막 체결가 (티커). 통상적인 '현재가' 정의이며 기본값.
    LAST = "last"
    #: 최우선 매수/매도 호가의 중간값. 체결이 뜸한 종목에서도 항상 최신.
    MID = "mid"


class Ticker(BaseModel):
    """마지막 체결가."""

    exchange: str = Field(..., description="거래소 ID")
    symbol: str = Field(..., description="통일 심볼 (예: BTC/KRW)")
    native_symbol: str = Field(..., description="거래소 원본 심볼 (예: KRW-BTC)")
    market_type: MarketType = Field(default=MarketType.SPOT, description="현물/선물 구분")
    base: str = Field(..., description="기준 통화")
    quote: str = Field(..., description="결제 통화")

    last_price: float = Field(..., description="마지막 체결가 (현재가)")
    timestamp: int = Field(
        ...,
        description=(
            "마지막 체결 시각 (epoch milliseconds). "
            "현재 시각과 크게 차이나면 거래가 뜸한 종목이라는 뜻이다"
        ),
    )

    latency_ms: float = Field(..., description="요청 시작~응답 파싱 완료까지 걸린 시간 (ms)")
