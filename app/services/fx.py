"""환율 서비스.

업비트는 KRW, 해외 거래소는 USDT 로 가격을 매기기 때문에, 두 가격을 비교하려면
공통 축으로 환산해야 한다.

기준 환율은 **업비트 KRW-USDT 마켓의 중간가**를 쓴다.
은행 고시 USD/KRW 환율이 아니라 실제 국내 시장에서 거래되는 테더 가격이므로,
"업비트에서 원화로 사서 해외에서 팔 때 실제로 얼마가 남는가" 라는 질문에
훨씬 가까운 값이다.

어느 거래소의 어느 마켓을 쓸지는 설정으로 바꿀 수 있다
(``krw_reference_exchange`` / ``fx_stablecoin``).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from app.core.config import settings
from app.core.errors import ExchangeAPIError
from app.exchanges.registry import get_exchange
from app.models.orderbook import MarketType
from app.models.symbol import Symbol
from app.models.ticker import PriceBasis


@dataclass(frozen=True, slots=True)
class FxRate:
    """스테이블코인 1개의 원화 가격."""

    #: USDT 1개당 원화 가격
    rate: float
    #: 어느 거래소의 어느 마켓에서 가져왔는지
    source: str
    #: 호가 기준 시각 (epoch ms)
    timestamp: int


class FxService:
    """원화 기준 거래소의 스테이블코인 시세에서 환율을 뽑아낸다.

    가격 기준(``PriceBasis``)에 따라 마지막 체결가 또는 호가 중간가를 쓴다.
    비교 대상 가격과 같은 기준을 써야 계산이 일관된다.
    """

    def __init__(self) -> None:
        # 기준별로 캐시를 따로 둔다 (last / mid 는 서로 다른 값이므로).
        self._cache: dict[PriceBasis, tuple[FxRate, float]] = {}

    @property
    def symbol(self) -> Symbol:
        """환율을 뽑아낼 마켓 (기본: USDT/KRW)."""
        return Symbol(base=settings.fx_stablecoin, quote=settings.krw_reference_quote)

    async def fetch_rate(self, basis: PriceBasis = PriceBasis.LAST) -> FxRate:
        """USDT/KRW 환율을 반환한다. 짧은 TTL 캐시가 적용된다."""
        now = time.monotonic()
        cached = self._cache.get(basis)
        if cached is not None and now - cached[1] < settings.fx_cache_ttl:
            return cached[0]

        exchange = get_exchange(settings.krw_reference_exchange)

        if basis is PriceBasis.LAST:
            ticker = await exchange.fetch_ticker(self.symbol, market_type=MarketType.SPOT)
            rate, timestamp = ticker.last_price, ticker.timestamp
            native_symbol, label = ticker.native_symbol, "last price"
        else:
            book = await exchange.fetch_orderbook(
                self.symbol, depth=1, market_type=MarketType.SPOT
            )
            rate, timestamp = book.mid_price, book.timestamp
            native_symbol, label = book.native_symbol, "mid price"

        if rate is None or rate <= 0:
            raise ExchangeAPIError(
                f"{exchange.name} {native_symbol} 시세에서 환율을 계산할 수 없습니다.",
                detail={"exchange": exchange.id, "native_symbol": native_symbol},
            )

        fx = FxRate(
            rate=rate,
            source=f"{exchange.id}:{native_symbol} ({label})",
            timestamp=timestamp,
        )
        self._cache[basis] = (fx, now)
        return fx


#: 캐시를 공유하기 위한 프로세스 단위 싱글턴.
fx_service = FxService()
