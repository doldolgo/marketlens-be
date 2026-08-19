"""매트릭스 서비스 — DB 스냅샷으로 코인별 최대 김프·역프를 계산한다.

거래소를 직접 호출하지 않는다. ``POST /refresh`` 가 저장해둔
``market_snapshots`` / ``usdkrw_rate`` 를 읽어서만 계산한다.

코인 하나마다
    - 모든 (국내 × 해외) 조합의 김프를 계산해 **가장 큰 김프** 조합을 고르고
    - 모든 조합의 역프를 계산해 **가장 큰 역프** 조합을 따로 고른다.
두 방향은 서로 다른 거래라 구매처·판매처가 방향마다 다를 수 있다.
"""

from __future__ import annotations

import time
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import MarketDataNotFoundError
from app.db import repository
from app.models.matrix import MatrixCoinEntry, MatrixDirection, MatrixResult
from app.models.orderbook import OrderBookLevel
from app.services.live_store import (
    AnySnapshot,
    received_at_ms,
    require_usdkrw_rate_or_db,
    snapshots_or_db,
)
from app.services.orderbook_walk import walk_by_amount, walk_by_quantity


def _to_krw(levels: list[OrderBookLevel], factor: float) -> list[OrderBookLevel]:
    """호가 가격을 원화로 환산한다."""
    if factor == 1.0:
        return levels
    return [OrderBookLevel(price=lv.price * factor, size=lv.size) for lv in levels]


def _epoch_ms(dt: datetime | None) -> int | None:
    return int(dt.timestamp() * 1000) if dt is not None else None


class MatrixService:
    """DB 스냅샷 기반 전 코인 매트릭스."""

    def _direction(
        self,
        *,
        buy_exchange: str,
        sell_exchange: str,
        buy_asks_krw: list[OrderBookLevel],
        sell_bids_krw: list[OrderBookLevel],
        amount_krw: float,
        withdrawal_available: bool,
        deposit_available: bool,
    ) -> MatrixDirection | None:
        """한 방향(한 조합)의 표면 프리미엄 · 실현 수익률 · 슬리피지를 계산한다.

        buy_asks_krw / sell_bids_krw 는 이미 원화로 환산된 호가다.
        """
        if not buy_asks_krw or not sell_bids_krw:
            return None

        best_ask = buy_asks_krw[0].price
        best_bid = sell_bids_krw[0].price
        if best_ask <= 0:
            return None

        # 표면 프리미엄 — 최우선 호가만 (금액과 무관)
        premium_percent = (best_bid / best_ask - 1) * 100

        # 실현 수익률 — 호가를 실제로 훑는다
        buy_walk = walk_by_amount(buy_asks_krw, amount_krw)
        if buy_walk.quantity <= 0:
            return None
        sell_walk = walk_by_quantity(sell_bids_krw, buy_walk.quantity)

        # 매도측 호가가 소진되면 **실제로 팔 수 있는 수량 기준으로 왕복을 맞춘다.**
        # 팔지 못한 코인을 수령액 0원으로 두면 단위당 손익과 무관한
        # -50~-70% 대의 무의미한 수익률이 나온다.
        if sell_walk.exhausted and 0 < sell_walk.quantity < buy_walk.quantity:
            buy_walk = walk_by_quantity(buy_asks_krw, sell_walk.quantity)

        spent = buy_walk.amount
        received = sell_walk.amount
        effective_percent = (received / spent - 1) * 100 if spent > 0 else 0.0

        return MatrixDirection(
            buy_exchange=buy_exchange,
            sell_exchange=sell_exchange,
            premium_percent=premium_percent,
            total_slippage_percent=premium_percent - effective_percent,
            withdrawal_available=withdrawal_available,
            deposit_available=deposit_available,
            depth_exhausted=buy_walk.exhausted or sell_walk.exhausted,
        )

    async def build(
        self, session: AsyncSession, *, amount_krw: float
    ) -> MatrixResult:
        """메모리(없으면 DB)를 읽어 전 코인 매트릭스를 만든다."""
        started = time.perf_counter()

        snapshots = await snapshots_or_db(session)

        if not snapshots:
            raise MarketDataNotFoundError(
                "시세 스냅샷이 없습니다. 먼저 POST /refresh 로 수집하세요.",
            )
        # 통일 환율 — 모든 조합이 같은 은행 고시 USD/KRW 를 쓴다.
        usdkrw = await require_usdkrw_rate_or_db(session)

        # 국내(KRW 가격)와 해외(USDT 가격)를 저장된 통화로 구분한다.
        domestic: dict[str, dict[str, AnySnapshot]] = {}
        overseas: dict[str, dict[str, AnySnapshot]] = {}
        for snap in snapshots:
            if snap.quote == settings.krw_reference_quote:
                domestic.setdefault(snap.base, {})[snap.exchange] = snap
            elif snap.quote == settings.overseas_quote:
                overseas.setdefault(snap.base, {})[snap.exchange] = snap

        excluded = {b.upper() for b in settings.scan_excluded_bases}
        warnings: list[str] = []

        entries: list[MatrixCoinEntry] = []
        combinations = 0
        oldest: datetime | None = None
        newest: datetime | None = None

        for base in sorted(set(domestic) & set(overseas)):
            if base in excluded:
                continue

            dom_snaps = domestic[base]
            ovs_snaps = overseas[base]

            best_fwd: MatrixDirection | None = None
            best_rev: MatrixDirection | None = None

            for dom in dom_snaps.values():
                dom_bids = repository.levels_from_json(dom.bids)
                dom_asks = repository.levels_from_json(dom.asks)
                rate = usdkrw.rate

                for ovs in ovs_snaps.values():
                    combinations += 1
                    ovs_asks_krw = _to_krw(
                        repository.levels_from_json(ovs.asks), rate
                    )
                    ovs_bids_krw = _to_krw(
                        repository.levels_from_json(ovs.bids), rate
                    )

                    # 김프 — 해외 매수 → 국내 매도.
                    # 코인을 옮겨야 하므로 해외 출금 + 국내 입금이 열려 있어야 한다.
                    fwd = self._direction(
                        buy_exchange=ovs.exchange,
                        sell_exchange=dom.exchange,
                        buy_asks_krw=ovs_asks_krw,
                        sell_bids_krw=dom_bids,
                        amount_krw=amount_krw,
                        withdrawal_available=ovs.withdrawal_enabled,
                        deposit_available=dom.deposit_enabled,
                    )
                    if fwd and (
                        best_fwd is None
                        or fwd.premium_percent > best_fwd.premium_percent
                    ):
                        best_fwd = fwd

                    # 역프 — 국내 매수 → 해외 매도.
                    rev = self._direction(
                        buy_exchange=dom.exchange,
                        sell_exchange=ovs.exchange,
                        buy_asks_krw=dom_asks,
                        sell_bids_krw=ovs_bids_krw,
                        amount_krw=amount_krw,
                        withdrawal_available=dom.withdrawal_enabled,
                        deposit_available=ovs.deposit_enabled,
                    )
                    if rev and (
                        best_rev is None
                        or rev.premium_percent > best_rev.premium_percent
                    ):
                        best_rev = rev

                    for snap in (dom, ovs):
                        if snap.updated_at is None:
                            continue
                        if oldest is None or snap.updated_at < oldest:
                            oldest = snap.updated_at
                        if newest is None or snap.updated_at > newest:
                            newest = snap.updated_at

            if best_fwd is None and best_rev is None:
                continue

            suspicious = best_fwd is not None and (
                abs(best_fwd.premium_percent) >= settings.scan_suspicious_percent
            )

            entries.append(
                MatrixCoinEntry(
                    sym=base,
                    fwd=best_fwd,
                    rev=best_rev,
                    suspicious=suspicious,
                )
            )

        # 김프 표면 프리미엄 내림차순. 김프 계산 불가(null)는 뒤로 보낸다.
        entries.sort(
            key=lambda e: (
                e.fwd.premium_percent if e.fwd else float("-inf")
            ),
            reverse=True,
        )

        if amount_krw > settings.orderbook_max_amount_krw:
            warnings.append(
                f"요청 금액({amount_krw:,.0f}원)이 호가 저장 한도"
                f"({settings.orderbook_max_amount_krw:,.0f}원)보다 큽니다. "
                "depth_exhausted 인 조합은 슬리피지가 실제보다 작게 계산됐습니다."
            )
        warnings.append("거래 수수료·출금 수수료·전송 시간은 반영되지 않았습니다.")
        if any(
            d is not None
            and not (d.withdrawal_available and d.deposit_available)
            for e in entries
            for d in (e.fwd, e.rev)
        ):
            warnings.append(
                "입출금이 막힌 것으로 표시된 조합이 있습니다. 실제 중단일 수도, "
                "수집 시점에 확인하지 못한 것일 수도 있습니다 — 확인 불가는 "
                "False 로 저장됩니다 (platform_status 의 실패율 참고)."
            )

        return MatrixResult(
            data_received_at=received_at_ms(snapshots),
            amount_krw=amount_krw,
            coins=entries,
            scanned_coins=len(entries),
            scanned_combinations=combinations,
            dom_list=sorted(
                {s.exchange for by_ex in domestic.values() for s in by_ex.values()}
            ),
            fx_list=sorted(
                {s.exchange for by_ex in overseas.values() for s in by_ex.values()}
            ),
            data_oldest_at=_epoch_ms(oldest),
            data_newest_at=_epoch_ms(newest),
            warnings=warnings,
            fetched_at=int(time.time() * 1000),
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
        )


matrix_service = MatrixService()
