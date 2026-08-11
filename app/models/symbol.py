"""통일 심볼(Unified Symbol) 파싱.

MarketLens 는 거래소와 무관하게 ``BASE/QUOTE`` 형식의 심볼을 사용한다.
각 Exchange 구현체가 이 값을 자신의 네이티브 심볼로 변환한다.

    BTC/KRW   -> Upbit:   KRW-BTC
    BTC/USDT  -> Binance: BTCUSDT
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.errors import InvalidSymbolError


@dataclass(frozen=True, slots=True)
class Symbol:
    """``BASE/QUOTE`` 형태의 통일 심볼. BASE를 QUOTE로 사겠다."""

    base: str
    quote: str

    @classmethod
    def parse(cls, raw: str) -> "Symbol":
        """문자열을 Symbol 로 변환한다.

        Args:
            raw: "BTC/KRW", "btc-krw", "BTC_KRW" 등. 구분자는 / - _ 를 허용한다.

        Raises:
            InvalidSymbolError: base/quote 두 조각으로 나눌 수 없는 경우.
        """
        normalized = raw.strip().upper().replace("-", "/").replace("_", "/")
        parts = [p for p in normalized.split("/") if p]

        if len(parts) != 2:
            raise InvalidSymbolError(
                f"심볼 형식이 올바르지 않습니다: {raw!r}. 'BASE/QUOTE' 형식이어야 합니다 (예: BTC/KRW)"
            )

        return cls(base=parts[0], quote=parts[1])

    def __str__(self) -> str:
        return f"{self.base}/{self.quote}"
