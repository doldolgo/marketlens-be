"""전종목 프리미엄 스캔 서비스 — DB 스냅샷 기반.

국내에 상장된 모든 코인을 훑어 **김프 1등**과 **역김프 1등**을 찾는다.

거래소를 직접 호출하지 않는다. ``POST /refresh`` 가 저장해둔
``market_snapshots`` 에서 **국내(KRW) 전종목 × 해외(USDT) 스냅샷의 교집합**을
돌며 두 방향의 수익률을 조합마다 계산한다. 환율은 통일 환율(``fx_rate``,
하나은행 고시 USD/KRW 매매기준율) 하나다.

수익률 계산식은 `/premium/fwd` · `/premium/rev` 와 **완전히 동일**하다.
가격은 체결되는 쪽 호가를 쓴다 — 살 때 매도호가(ask), 팔 때 매수호가(bid).
금액 기반 계산은 `/matrix` 가 담당한다.
"""

from __future__ import annotations

import time
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import MarketDataNotFoundError
from app.db import repository
from app.db.models import MarketSnapshot
from app.exchanges.registry import get_exchange
from app.models.premium import PremiumDirection
from app.models.scan import ScanEntry, ScanResult, SortOrder
from app.models.ticker import PriceSide
from app.services.premium_service import (
    exchange_name,
    premium_service,
    resolve_fx_rate,
    resolve_side,
)

#: 유동성이 이보다 얇으면 경고를 붙인다 (원화).
THIN_LIQUIDITY_KRW = 1_000_000.0

SUSPICION_REASON = (
    "프리미엄이 비정상적으로 큽니다. 대부분 (1) 티커는 같지만 서로 다른 프로젝트이거나 "
    "(2) 한쪽 거래소에서 입출금이 막혀 가격이 따로 노는 경우입니다. "
    "거래 전 양쪽 거래소에서 같은 코인이 맞는지 반드시 확인하세요."
)


def _pick(
    snap: MarketSnapshot, side: PriceSide
) -> tuple[float, float | None] | None:
    """스냅샷에서 원하는 side 의 (가격, 최우선 호가 잔량) 을 꺼낸다. 없으면 None.

    잔량은 유동성 필터에 쓴다.
    호가는 저장 시점에 이미 정렬되어 있으므로 첫 단계가 최우선 호가다.
    """
    levels = snap.bids if side is PriceSide.BID else snap.asks  # [[가격, 잔량], ...]
    return (float(levels[0][0]), float(levels[0][1])) if levels else None


def _epoch_ms(dt: datetime | None) -> int | None:
    return int(dt.timestamp() * 1000) if dt is not None else None


class ScanService:
    """전종목을 훑어 방향별 최대 프리미엄을 찾는다. 데이터는 전부 DB."""

    def _build_entry(
        self,
        base: str,
        direction: PremiumDirection,
        domestic_id: str,
        dom_snap: MarketSnapshot,
        domestic_side: PriceSide,
        ovs_snap: MarketSnapshot,
        overseas_side: PriceSide,
        usd_krw_rate: float,
    ) -> ScanEntry | None:
        """한 조합의 프리미엄을 계산한다. 값이 부족하면 None."""
        dom = _pick(dom_snap, domestic_side)
        ovs = _pick(ovs_snap, overseas_side)
        if dom is None or ovs is None:
            return None

        domestic_price, domestic_size = dom
        overseas_price, overseas_size = ovs
        overseas_krw = overseas_price * usd_krw_rate

        # /premium 과 동일한 공식
        if direction is PremiumDirection.FWD:
            buy_price, sell_price = overseas_krw, domestic_price
        else:
            buy_price, sell_price = domestic_price, overseas_krw

        if buy_price <= 0:
            return None
        ratio = sell_price / buy_price

        # 유동성: 양쪽 최우선 호가 잔량을 원화로 환산해 작은 쪽
        liquidity: float | None = None
        if domestic_size is not None and overseas_size is not None:
            liquidity = min(
                domestic_size * domestic_price,
                overseas_size * overseas_krw,
            )

        premium_percent = (ratio - 1) * 100
        suspicious = abs(premium_percent) >= settings.scan_suspicious_percent

        return ScanEntry(
            sym=base,
            direction=direction,
            dom=domestic_id,
            dom_price=domestic_price,
            fx=ovs_snap.exchange,
            fx_name=exchange_name(ovs_snap.exchange),
            usd=overseas_price,
            premium_percent=premium_percent,
            premium_krw=sell_price - buy_price,
            liquidity_krw=liquidity,
            suspicious=suspicious,
            suspicion_reason=SUSPICION_REASON if suspicious else None,
        )

    async def scan(
        self,
        session: AsyncSession,
        *,
        domestic: str | None = None,
        exchanges: list[str] | None = None,
        min_liquidity_krw: float = 0.0,
        limit: int = 10,
        order: SortOrder = SortOrder.ASC,
    ) -> ScanResult:
        """전종목을 훑어 김프 1등과 역김프 1등을 찾는다. 데이터는 전부 DB.

        가격은 체결되는 쪽 호가를 쓴다 — 살 때 매도호가(ask), 팔 때 매수호가(bid).

        Args:
            session: DB 세션.
            domestic: 국내 기준 거래소 ID (업비트/빗썸 등). 생략하면 설정 기본값.
            exchanges: 비교할 해외 거래소 ID 목록. 생략하면 DB 에 USDT 스냅샷이
                있는 전체.
            min_liquidity_krw: 저장된 최우선 호가의 체결 가능 금액(잔량 × 가격)이
                이보다 작으면 제외.
            limit: 방향별 목록 개수.
            order: 목록 정렬 방향. ``asc`` 면 수익률 오름차순 (기본).

        Raises:
            MarketDataNotFoundError: DB 에 스캔할 스냅샷이나 환율이 없는 경우.
        """
        started = time.perf_counter()

        domestic_id = premium_service.resolve_domestic(domestic)
        rate = await resolve_fx_rate(session)

        # 스냅샷 전체를 한 번에 읽어 국내(KRW)와 해외(USDT)로 나눈다.
        snapshots = await repository.get_snapshots(session)
        domestic_snaps: dict[str, MarketSnapshot] = {}
        overseas_snaps: dict[str, dict[str, MarketSnapshot]] = {}
        for snap in snapshots:
            if (
                snap.exchange == domestic_id
                and snap.quote == settings.krw_reference_quote
            ):
                domestic_snaps[snap.base] = snap
            elif (
                snap.quote == settings.fx_stablecoin
                and snap.exchange != domestic_id
            ):
                overseas_snaps.setdefault(snap.exchange, {})[snap.base] = snap

        warnings: list[str] = []
        if exchanges is not None:
            overseas_ids = []
            for exchange_id in exchanges:
                # 등록되지 않은 거래소 ID 는 여기서 404 로 걸러진다.
                eid = get_exchange(exchange_id).id
                overseas_ids.append(eid)
                if not overseas_snaps.get(eid):
                    warnings.append(
                        f"{eid} 거래소의 {settings.fx_stablecoin} 스냅샷이 DB 에 "
                        "없어 스캔에서 빠졌습니다. POST /refresh 로 수집했는지 "
                        "확인하세요."
                    )
        else:
            overseas_ids = sorted(overseas_snaps)

        if not domestic_snaps:
            raise MarketDataNotFoundError(
                f"DB 에 {domestic_id} 거래소의 {settings.krw_reference_quote} "
                "스냅샷이 없습니다. 먼저 POST /refresh 로 수집하세요.",
                detail={"exchange": domestic_id},
            )
        if not any(overseas_snaps.get(eid) for eid in overseas_ids):
            raise MarketDataNotFoundError(
                "DB 에 스캔할 해외(USDT) 스냅샷이 없습니다. "
                "먼저 POST /refresh 로 수집하세요.",
                detail={"overseas": overseas_ids},
            )

        fwd_entries: list[ScanEntry] = []
        rev_entries: list[ScanEntry] = []
        coins: set[str] = set()
        pairs = 0
        filtered_out = 0
        oldest: datetime | None = None
        newest: datetime | None = None

        excluded = {b.upper() for b in settings.scan_excluded_bases}

        for overseas_id in overseas_ids:
            for base, overseas in overseas_snaps.get(overseas_id, {}).items():
                if base in excluded:
                    continue  # 티커 충돌이 확인된 코인
                dom = domestic_snaps.get(base)
                if dom is None:
                    continue  # 국내 미상장

                coins.add(base)
                pairs += 1

                for snap in (dom, overseas):
                    if snap.updated_at is None:
                        continue
                    if oldest is None or snap.updated_at < oldest:
                        oldest = snap.updated_at
                    if newest is None or snap.updated_at > newest:
                        newest = snap.updated_at

                for direction in (PremiumDirection.FWD, PremiumDirection.REV):
                    is_fwd = direction is PremiumDirection.FWD
                    entry = self._build_entry(
                        base,
                        direction,
                        domestic_id,
                        dom,
                        resolve_side(is_buy=not is_fwd),
                        overseas,
                        resolve_side(is_buy=is_fwd),
                        rate.rate,
                    )
                    if entry is None:
                        continue
                    if (
                        min_liquidity_krw > 0
                        and entry.liquidity_krw is not None
                        and entry.liquidity_krw < min_liquidity_krw
                    ):
                        filtered_out += 1
                        continue

                    (fwd_entries if is_fwd else rev_entries).append(entry)

        # best_* 는 언제나 '최대 수익률' 이다 (정렬 옵션과 무관).
        best_fwd = max(fwd_entries, key=lambda e: e.premium_percent, default=None)
        best_rev = max(
            rev_entries, key=lambda e: e.premium_percent, default=None
        )

        # top_* 는 언제나 **수익률 상위 limit 개**를 고른 뒤, 그 안에서만
        # 요청한 방향으로 정렬한다. 전체를 오름차순 정렬한 뒤 자르면
        # '상위 목록'이 아니라 최하위 N개가 나가버린다.
        descending = order is SortOrder.DESC

        def _top(entries: list[ScanEntry]) -> list[ScanEntry]:
            top = sorted(entries, key=lambda e: e.premium_percent, reverse=True)[
                :limit
            ]
            top.sort(key=lambda e: e.premium_percent, reverse=descending)
            return top

        top_fwd = _top(fwd_entries)
        top_rev = _top(rev_entries)

        suspicious_count = sum(
            1 for e in (*fwd_entries, *rev_entries) if e.suspicious
        )

        for label, best in (("김프", best_fwd), ("역김프", best_rev)):
            if best is not None and best.suspicious:
                warnings.append(
                    f"{label} 1위 {best.sym} ({best.premium_percent:+.2f}%) 는 "
                    "의심 항목입니다. 티커가 같아도 서로 다른 코인일 수 있습니다 — "
                    "예: 업비트 KRW-AI 는 젠신(Gensyn) 입니다."
                )
        for label, best in (("김프", best_fwd), ("역김프", best_rev)):
            if (
                best is not None
                and best.liquidity_krw is not None
                and best.liquidity_krw < THIN_LIQUIDITY_KRW
            ):
                warnings.append(
                    f"{label} 1위 {best.sym} 의 최우선 호가 체결 가능 금액이 "
                    f"{best.liquidity_krw:,.0f}원뿐입니다. 실제로는 이 수익률에 "
                    "거의 체결되지 않습니다."
                )
        warnings.append(
            "거래 수수료·출금 수수료·전송 시간이 반영되지 않은 이론값입니다. "
            "최우선 호가 1단계만 보므로 금액을 넣었을 때의 수익은 /matrix 나 "
            "/arbitrage 로 확인하세요."
        )

        return ScanResult(
            order=order,
            dom=domestic_id,
            fx_list=overseas_ids,
            usd_krw_rate=rate.rate,
            rate_updated_at=_epoch_ms(rate.updated_at),
            scanned_coins=len(coins),
            scanned_pairs=pairs,
            filtered_out=filtered_out,
            excluded_bases=sorted(excluded),
            suspicious_count=suspicious_count,
            best_fwd=best_fwd,
            best_rev=best_rev,
            top_fwd=top_fwd,
            top_rev=top_rev,
            data_oldest_at=_epoch_ms(oldest),
            data_newest_at=_epoch_ms(newest),
            warnings=warnings,
            fetched_at=int(time.time() * 1000),
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
        )


scan_service = ScanService()
