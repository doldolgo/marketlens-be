"""김치 프리미엄 / 역프리미엄 계산 서비스 — DB 스냅샷 기반.

거래소를 직접 호출하지 않는다. ``POST /refresh`` 가 저장해둔
``market_snapshots`` / ``fx_rate`` 를 읽어서만 계산한다.

두 방향은 **서로 다른 거래**다.

    김프   : 해외 매수 → 국내 매도   수익률 = 국내_매도가 / 해외_매수가(원화환산) - 1
    역김프 : 국내 매수 → 해외 매도   수익률 = 해외_매도가(원화환산) / 국내_매수가 - 1

국내 쪽은 원화 기준 거래소(기본 업비트) 하나로 고정된다.
해외 쪽은 요청한 거래소들이며, 생략하면 DB 에 USDT 스냅샷이 있는 모든 거래소를 쓴다.

가격은 항상 **실제로 체결되는 쪽 호가**를 저장된 스냅샷에서 뽑는다 —
살 때는 매도호가(ask), 팔 때는 매수호가(bid). 방향에 따라 쓰는 호가가
달라지므로 김프/역김프 값은 서로 독립적이다.

환율은 ``fx_rate`` 에 저장된 **하나은행 고시 USD/KRW 매매기준율 하나**다.
예전의 국내 거래소별 KRW-USDT 시세(테더 프리미엄이 섞인 값) 대신, 어느
국내 거래소를 기준으로 하든 같은 은행 환율을 쓴다.
"""

from __future__ import annotations

import time
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import (
    InvalidRequestError,
    MarketDataNotFoundError,
    UnsupportedExchangeError,
)
from app.db import repository
from app.db.models import FxRate, MarketSnapshot
from app.exchanges.registry import domestic_exchange_ids, get_exchange
from app.models.premium import (
    PremiumDirection,
    PremiumEntry,
    PremiumFailure,
    PremiumResult,
)
from app.models.ticker import PriceSide

def resolve_side(*, is_buy: bool) -> PriceSide:
    """매수/매도 여부로 어느 호가를 집을지 결정한다.

    살 때는 매도호가(ask)에 체결되고, 팔 때는 매수호가(bid)에 체결된다.
    """
    return PriceSide.ASK if is_buy else PriceSide.BID


def snapshot_price(snap: MarketSnapshot, side: PriceSide) -> float | None:
    """스냅샷에서 ``side`` 에 해당하는 최우선 호가를 꺼낸다. 없으면 None."""
    if side is PriceSide.BID:
        bids = repository.levels_from_json(snap.bids)
        return bids[0].price if bids else None
    asks = repository.levels_from_json(snap.asks)
    return asks[0].price if asks else None


async def resolve_fx_rate(session: AsyncSession) -> FxRate:
    """통일 환율(하나은행 고시 USD/KRW 매매기준율)을 DB 에서 가져온다.

    거래소별 환율 개념이 없어졌으므로 어떤 계산이든 이 한 값을 쓴다.
    없으면(수집 전) 404 성격의 도메인 예외를 던진다.
    """
    return await repository.require_fx_rate(session)


def exchange_name(exchange_id: str) -> str:
    """거래소 ID 를 표시용 이름으로. 레지스트리에 없으면 ID 그대로."""
    try:
        return get_exchange(exchange_id).name
    except UnsupportedExchangeError:
        return exchange_id


def _epoch_ms(dt: datetime | None) -> int | None:
    return int(dt.timestamp() * 1000) if dt is not None else None


class PremiumService:
    """국내 가격과 해외 가격의 방향별 괴리를 계산한다. 데이터는 전부 DB."""

    def resolve_domestic(self, domestic: str | None) -> str:
        """국내 기준 거래소를 결정한다.

        Raises:
            InvalidRequestError: 국내(원화) 거래소가 아닌 곳을 지정한 경우.
        """
        if domestic is None:
            return settings.krw_reference_exchange

        exchange = get_exchange(domestic)
        if not exchange.is_domestic:
            raise InvalidRequestError(
                f"{exchange.name}는 원화 거래소가 아니라 김프의 국내 축이 될 수 없습니다. "
                f"선택 가능: {', '.join(domestic_exchange_ids())}",
                detail={"exchange": exchange.id, "domestic": domestic_exchange_ids()},
            )
        return exchange.id

    async def _overseas_snapshots(
        self,
        session: AsyncSession,
        base: str,
        exchanges: list[str] | None,
        domestic_id: str,
        failures: list[PremiumFailure],
    ) -> list[MarketSnapshot]:
        """비교 대상 해외(USDT) 스냅샷을 모은다.

        ``exchanges`` 생략 시 DB 에 있는 USDT 스냅샷 전체 (국내 기준 거래소 제외).
        명시했다면 거래소 ID 를 검증하고, 스냅샷이 없는 곳은 failures 에 기록한다.
        """
        if exchanges is None:
            snaps = await repository.get_snapshots(session, base=base)
            return [
                s
                for s in snaps
                if s.quote == settings.fx_stablecoin and s.exchange != domestic_id
            ]

        symbol_str = f"{base.upper()}/{settings.fx_stablecoin}"
        found: list[MarketSnapshot] = []
        for exchange_id in exchanges:
            # 등록되지 않은 거래소 ID 는 여기서 404 로 걸러진다.
            eid = get_exchange(exchange_id).id
            snap = await repository.get_snapshot(session, eid, base)
            if snap is None or snap.quote != settings.fx_stablecoin:
                failures.append(
                    PremiumFailure(
                        exchange=eid,
                        symbol=symbol_str,
                        error_code=MarketDataNotFoundError.code,
                        message=(
                            f"DB 에 {eid} 거래소의 {base.upper()} "
                            f"{settings.fx_stablecoin} 마켓 스냅샷이 없습니다. "
                            "POST /refresh 로 수집했는지 확인하세요."
                        ),
                    )
                )
                continue
            found.append(snap)
        return found

    def _build_entry(
        self,
        snap: MarketSnapshot,
        overseas_price: float,
        domestic_price: float,
        usd_krw_rate: float,
        direction: PremiumDirection,
    ) -> PremiumEntry:
        """해외 거래소 하나와의 프리미엄을 계산한다.

        해외 가격은 USDT 표시지만 USDT≈USD 페그를 전제로 은행 USD/KRW
        환율을 곱해 원화 환산한다 — 김프 사이트들의 표준 계산 방식이다.
        """
        overseas_krw = overseas_price * usd_krw_rate

        # 방향에 따라 무엇이 매수측이고 무엇이 매도측인지 결정된다.
        if direction is PremiumDirection.FWD:
            buy_price, sell_price = (
                overseas_krw,
                domestic_price,
            )  # 해외에서 사서 국내에 판다
        else:
            buy_price, sell_price = (
                domestic_price,
                overseas_krw,
            )  # 국내에서 사서 해외에 판다

        ratio = sell_price / buy_price

        return PremiumEntry(
            fx=snap.exchange,
            fx_name=exchange_name(snap.exchange),
            usd=overseas_price,
            premium_percent=(ratio - 1) * 100,
            premium_krw=sell_price - buy_price,
            profitable=ratio > 1,
            data_updated_at=_epoch_ms(snap.updated_at),
        )

    async def fetch_premiums(
        self,
        session: AsyncSession,
        base: str,
        *,
        direction: PremiumDirection,
        domestic: str | None = None,
        exchanges: list[str] | None = None,
    ) -> PremiumResult:
        """한 방향의 프리미엄을 거래소별로 계산한다. 데이터는 전부 DB.

        가격은 체결되는 쪽 호가를 쓴다 — 살 때 매도호가(ask), 팔 때 매수호가(bid).

        Args:
            session: DB 세션.
            base: 코인 심볼 (예: "BTC").
            direction: 차익 방향. 김프(해외→국내) 또는 역김프(국내→해외).
            domestic: 국내 기준 거래소 ID (업비트/빗썸 등). 생략하면 설정 기본값.
            exchanges: 비교할 해외 거래소 ID 목록. 생략하면 DB 에 USDT 스냅샷이
                있는 전체.

        Raises:
            MarketDataNotFoundError: 국내 스냅샷이나 환율이 DB 에 없는 경우.
                이 둘은 계산의 기준이라 하나라도 없으면 아무것도 계산할 수 없다.
        """
        started = time.perf_counter()
        domestic_id = self.resolve_domestic(domestic)

        dom_snap = await repository.require_snapshot(session, domestic_id, base)
        if dom_snap.quote != settings.krw_reference_quote:
            raise MarketDataNotFoundError(
                f"DB 에 {domestic_id} 거래소의 {base.upper()} "
                f"{settings.krw_reference_quote} 마켓 스냅샷이 없습니다.",
                detail={"exchange": domestic_id, "base": base.upper()},
            )
        rate = await resolve_fx_rate(session)

        failures: list[PremiumFailure] = []
        overseas_snaps = await self._overseas_snapshots(
            session, base, exchanges, domestic_id, failures
        )

        # 방향에 따라 어느 쪽에서 사고 파는지가 정해지고, 그에 맞는 호가를 집는다.
        is_fwd = direction is PremiumDirection.FWD
        overseas_side = resolve_side(is_buy=is_fwd)  # 김프면 해외에서 매수
        domestic_side = resolve_side(is_buy=not is_fwd)  # 김프면 국내에서 매도

        domestic_price = snapshot_price(dom_snap, domestic_side)
        if domestic_price is None or domestic_price <= 0:
            raise MarketDataNotFoundError(
                f"{domestic_id} 의 {base.upper()} 스냅샷에서 {domestic_side.value} "
                "가격을 얻을 수 없습니다. 저장된 호가가 비어 있습니다 — "
                "POST /refresh 로 다시 수집하세요.",
                detail={
                    "exchange": domestic_id,
                    "base": base.upper(),
                    "side": domestic_side.value,
                },
            )

        entries: list[PremiumEntry] = []
        used: list[MarketSnapshot] = [dom_snap]

        for snap in sorted(overseas_snaps, key=lambda s: s.exchange):
            price = snapshot_price(snap, overseas_side)
            if price is None or price <= 0:
                failures.append(
                    PremiumFailure(
                        exchange=snap.exchange,
                        symbol=f"{snap.base}/{snap.quote}",
                        error_code=MarketDataNotFoundError.code,
                        message=(
                            f"{snap.exchange} 의 {snap.base} 스냅샷에서 "
                            f"{overseas_side.value} 가격을 얻을 수 없습니다. "
                            "저장된 호가가 비어 있습니다."
                        ),
                    )
                )
                continue
            entries.append(
                self._build_entry(snap, price, domestic_price, rate.rate, direction)
            )
            used.append(snap)

        entries.sort(key=lambda e: e.premium_percent, reverse=True)

        stamps = [s.updated_at for s in used if s.updated_at is not None]

        return PremiumResult(
            sym=base.upper(),
            direction=direction,
            dom=dom_snap.exchange,
            dom_price=domestic_price,
            usd_krw_rate=rate.rate,
            rate_updated_at=_epoch_ms(rate.updated_at),
            premiums=entries,
            failures=failures,
            data_oldest_at=_epoch_ms(min(stamps) if stamps else None),
            data_newest_at=_epoch_ms(max(stamps) if stamps else None),
            fetched_at=int(time.time() * 1000),
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
        )


premium_service = PremiumService()
