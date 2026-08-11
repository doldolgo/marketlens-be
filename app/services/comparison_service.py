"""거래소 간 가격 비교 서비스 — DB 스냅샷 기반.

거래소를 직접 호출하지 않는다. ``POST /refresh`` 가 저장해둔
``market_snapshots`` / ``krw_rates`` 를 읽어서만 계산한다.

동작 순서
    1. 요청받은 코인의 스냅샷을 DB 에서 전부 읽는다 (거래소 필터 적용).
    2. 각 행의 가격(마지막 체결가)을 공통 통화로 환산한다.
       - KRW 기준: USDT 행에 기준 국내 거래소(업비트) 환율을 곱한다.
       - USDT 기준: KRW 행을 그 국내 거래소 자기 환율로 나눈다 (없으면 기준 환율).
    3. 최저 매수처 / 최고 매도처를 찾아 스프레드를 계산한다.
"""

from __future__ import annotations

import time
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import InvalidRequestError, MarketDataNotFoundError
from app.db import repository
from app.db.models import KrwRate, MarketSnapshot
from app.exchanges.registry import get_exchange
from app.models.comparison import ArbitrageSpread, ComparisonResult, ExchangeQuote

#: 공통 통화로 환산 가능한 결제 통화. (수집기도 이 두 통화만 저장한다)
_CONVERTIBLE = frozenset({"KRW", "USDT"})


def _epoch_ms(dt: datetime | None) -> int | None:
    return int(dt.timestamp() * 1000) if dt is not None else None


class ComparisonService:
    """여러 거래소의 같은 코인 가격을 공통 통화 기준으로 비교한다."""

    def _conversion(
        self,
        snap: MarketSnapshot,
        currency: str,
        rates: dict[str, KrwRate],
        reference_rate: KrwRate | None,
    ) -> tuple[float, float | None]:
        """(환산 계수, 적용 환율) 을 구한다.

        계수를 원래 통화 가격에 곱하면 공통 통화 가격이 된다.

        Raises:
            MarketDataNotFoundError: 환산이 필요한데 DB 에 환율이 없는 경우.
        """
        if snap.quote == currency:
            return 1.0, None

        if currency == "KRW":
            # USDT 행 → KRW: 기준 국내 거래소 환율을 곱한다.
            rate = reference_rate
        else:
            # KRW 행 → USDT: 그 국내 거래소 자기 환율로 나눈다. 없으면 기준 환율.
            rate = rates.get(snap.exchange) or reference_rate

        # 수집기가 0 이하 환율을 저장하지 않지만, 수동으로 오염된 DB 에서도
        # ZeroDivisionError 500 대신 명확한 404 가 나가도록 방어한다.
        if rate is None or rate.rate <= 0:
            raise MarketDataNotFoundError(
                "DB 에 유효한 KRW-USDT 환율이 없어 통화를 환산할 수 없습니다. "
                "먼저 POST /refresh 로 수집하세요.",
                detail={"exchange": snap.exchange, "quote": snap.quote},
            )
        factor = rate.rate if currency == "KRW" else 1.0 / rate.rate
        return factor, rate.rate

    def _to_quote(self, snap: MarketSnapshot, factor: float) -> ExchangeQuote:
        """스냅샷 한 행을 공통 통화로 환산한 ExchangeQuote 로 변환한다."""
        best_bid = float(snap.bids[0][0]) if snap.bids else None
        best_ask = float(snap.asks[0][0]) if snap.asks else None

        return ExchangeQuote(
            exchange=snap.exchange,
            quote_currency=snap.quote,
            price=snap.price * factor,
            best_bid=best_bid * factor if best_bid is not None else None,
            best_ask=best_ask * factor if best_ask is not None else None,
            data_updated_at=_epoch_ms(snap.updated_at),
        )

    def _build_spread(self, quotes: list[ExchangeQuote]) -> ArbitrageSpread | None:
        """가장 싸게 사는 곳과 가장 비싸게 파는 곳의 차이를 계산한다.

        호가가 저장된 거래소가 2곳 미만이면 None 을 반환한다.
        수수료/출금비용/전송시간은 반영하지 않은 이론값이다.
        """
        priced = [
            q for q in quotes if q.best_ask is not None and q.best_bid is not None
        ]
        if len(priced) < 2:
            return None

        buy = min(priced, key=lambda q: q.best_ask)
        sell = max(priced, key=lambda q: q.best_bid)

        if buy.exchange == sell.exchange:
            return None

        absolute = sell.best_bid - buy.best_ask
        percent = (absolute / buy.best_ask) * 100 if buy.best_ask else 0.0

        return ArbitrageSpread(
            buy_exchange=buy.exchange,
            buy_price=buy.best_ask,
            sell_exchange=sell.exchange,
            sell_price=sell.best_bid,
            absolute=absolute,
            percent=percent,
        )

    async def compare(
        self,
        session: AsyncSession,
        base: str,
        *,
        exchanges: list[str] | None = None,
        common_currency: str = "KRW",
    ) -> ComparisonResult:
        """거래소 간 가격을 비교한다.

        Args:
            session: DB 세션.
            base: 비교할 코인 (예: "BTC").
            exchanges: 비교할 거래소 ID 목록. 생략하면 스냅샷이 있는 전체.
            common_currency: 환산 기준 통화 ("KRW" 또는 "USDT").

        Raises:
            MarketDataNotFoundError: 비교할 스냅샷이 DB 에 하나도 없는 경우.
        """
        started = time.perf_counter()
        currency = common_currency.upper()
        if currency not in _CONVERTIBLE:
            raise InvalidRequestError(
                f"지원하지 않는 비교 기준 통화입니다: {currency}. "
                f"{' 또는 '.join(sorted(_CONVERTIBLE))} 만 지원합니다.",
                detail={"common_currency": currency},
            )

        snapshots = await repository.get_snapshots(session, base=base)

        missing: list[str] = []
        if exchanges is not None:
            # 레지스트리는 ID 검증(등록된 거래소인지)에만 쓴다 — 거래소 호출은 없다.
            wanted = list(dict.fromkeys(get_exchange(eid).id for eid in exchanges))
            by_exchange = {s.exchange: s for s in snapshots}
            snapshots = [by_exchange[eid] for eid in wanted if eid in by_exchange]
            missing = [eid for eid in wanted if eid not in by_exchange]

        if not snapshots:
            raise MarketDataNotFoundError(
                f"DB 에 {base.upper()} 스냅샷이 없습니다. "
                "POST /refresh 로 데이터를 수집했는지, 해당 거래소에 상장된 "
                "코인인지 확인하세요.",
                detail={"base": base.upper(), "missing_exchanges": missing},
            )

        rates = {r.exchange: r for r in await repository.get_krw_rates(session)}
        reference_rate = rates.get(settings.krw_reference_exchange) or (
            next(iter(rates.values())) if rates else None
        )

        quotes: list[ExchangeQuote] = []
        oldest: datetime | None = None
        newest: datetime | None = None

        for snap in snapshots:
            if snap.quote not in _CONVERTIBLE:
                continue  # BTC 마켓 등 — 현재 수집기는 저장하지 않는다

            factor, _ = self._conversion(snap, currency, rates, reference_rate)
            quotes.append(self._to_quote(snap, factor))

            if snap.updated_at is None:
                continue
            if oldest is None or snap.updated_at < oldest:
                oldest = snap.updated_at
            if newest is None or snap.updated_at > newest:
                newest = snap.updated_at

        quotes.sort(key=lambda q: q.price)

        return ComparisonResult(
            sym=base.upper(),
            common_currency=currency,
            usdt_krw_rate=reference_rate.rate if reference_rate else None,
            rate_exchange=reference_rate.exchange if reference_rate else None,
            quotes=quotes,
            missing_exchanges=missing,
            spread=self._build_spread(quotes),
            data_oldest_at=_epoch_ms(oldest),
            data_newest_at=_epoch_ms(newest),
            fetched_at=int(time.time() * 1000),
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
        )


comparison_service = ComparisonService()
