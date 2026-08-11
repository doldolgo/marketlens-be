"""거래소 전종목 일괄 시세 스냅샷.

코인 하나씩 조회하면 전종목 스캔이 불가능하다 (업비트 초당 10회 제한).
다행히 두 거래소 모두 **한 번의 호출로 전종목**을 내려주는 엔드포인트가 있다.

    업비트   GET /v1/ticker?markets=KRW-BTC,KRW-ETH,...   281개를 1회 호출로
    업비트   GET /v1/orderbook?markets=...                 (100개씩 나눠 호출)
    바이낸스 GET /api/v3/ticker/price                      3,683개 · weight 4
    바이낸스 GET /api/v3/ticker/bookTicker                 3,683개 · weight 4

``BulkQuote`` 는 그 결과를 코인 하나 단위로 담는다. 어떤 필드가 채워지는지는
무엇을 조회했느냐에 달렸다 (체결가 조회면 ``last``, 호가 조회면 ``bid``/``ask``).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BulkQuote:
    """전종목 조회에서 얻은 코인 하나의 시세."""

    #: 기준 통화 (예: BTC)
    base: str
    #: 결제 통화 (예: KRW, USDT)
    quote: str
    #: 거래소 원본 심볼 (예: KRW-BTC, BTCUSDT)
    native_symbol: str

    #: 마지막 체결가. 체결가 조회일 때만 채워진다.
    last: float | None = None
    #: 최우선 매수호가. 호가 조회일 때만 채워진다.
    bid: float | None = None
    #: 최우선 매도호가. 호가 조회일 때만 채워진다.
    ask: float | None = None
    #: 최우선 매수호가 잔량 (base 통화). 유동성 판단용.
    bid_size: float | None = None
    #: 최우선 매도호가 잔량 (base 통화).
    ask_size: float | None = None

    @property
    def mid(self) -> float | None:
        """최우선 호가 중간값."""
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2
