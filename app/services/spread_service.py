"""스프레드 테이블 서비스 — 메모리 스냅샷 기반.

(국내 거래소 × 해외 거래소 × 코인) 페어마다 김프(fwd)와 역프(rev)를
**한 행에 함께** 계산한다. FE 스프레드 탭이 이 결과를 그대로 그린다.

수익률 계산식은 `/premium/fwd` · `/premium/rev` 와 완전히 동일하다.
가격은 체결되는 쪽 호가 — 살 때 매도호가(ask), 팔 때 매수호가(bid).

유동성(liqDom / liqFx)은 최우선 호가의 체결 가능 금액이다. 슬리피지 추정용이라
매수·매도 양쪽 중 **작은 쪽**을 USD(T) 기준으로 담는다.

거래소를 직접 호출하지 않는다. 수집 사이클이 메모리(:mod:`app.services.live_store`)
에 올려둔 최신 스냅샷·환율을 읽어서만 계산한다 (재기동 직후 첫 사이클 전에는
DB 로 폴백한다).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import MarketDataNotFoundError
from app.exchanges.private.network_match import Verdict, choose, from_json
from app.models.spread import FeedStatus, SpreadRow, SpreadsResult
from app.services.live_store import (
    AnyRate,
    AnySnapshot,
    rate_for,
    received_at_ms,
    require_usdkrw_rate_or_db,
    snapshots_or_db,
    usdkrw_rates_or_db,
)


def _top_level(levels: list) -> tuple[float, float] | None:
    """저장 호가의 최우선 (가격, 잔량). 비어 있으면 None."""
    return (float(levels[0][0]), float(levels[0][1])) if levels else None


def _age_seconds(*stamps: datetime | None) -> float:
    """스냅샷 갱신 시각들 중 가장 오래된 것 기준 경과 초. 모르면 0.

    PostgreSQL(timezone=True)은 aware, SQLite(테스트)는 naive **UTC** 로
    돌려준다 — naive 를 로컬 시간으로 해석하면 시차만큼 age 가 틀어지므로
    UTC 로 못박는다.
    """
    known = [s for s in stamps if s is not None]
    if not known:
        return 0.0
    oldest = min(known)
    if oldest.tzinfo is None:
        oldest = oldest.replace(tzinfo=timezone.utc)
    return max(0.0, time.time() - oldest.timestamp())


class SpreadService:
    """전 페어의 김프/역프를 FE SpreadRow 형태로 만든다. 데이터는 전부 메모리."""

    @staticmethod
    def _transfer_state(
        dom_snap: AnySnapshot, fx_snap: AnySnapshot
    ) -> tuple[bool | None, bool | None, bool | None, bool | None, str | None]:
        """이 페어를 **실제로 옮길 수 있는지**를 네트워크까지 보고 판정한다.

        코인 단위 입출금만 보면 틀린다 — 바이낸스 GRT 는 Arbitrum 출금이
        열려 "가능"이지만 업비트는 Ethereum 으로만 받으므로 옮길 수 없다.
        국내가 지원하는 망을 기준으로 잡고, 해외에서 그 망을 찾아 상태를 읽는다.

        Returns:
            (국내입금, 국내출금, 해외입금, 해외출금, 망 이름)
        """
        dom_nets = from_json(dom_snap.networks)
        fx_nets = from_json(fx_snap.networks)

        # 망 정보가 아예 없으면 예전처럼 코인 단위 값을 쓴다. 정보가 늘기
        # 전보다 나빠질 이유는 없다 (수집 직후 한 사이클 동안의 과도기).
        if not dom_nets:
            return (
                dom_snap.deposit_enabled,
                dom_snap.withdrawal_enabled,
                fx_snap.deposit_enabled,
                fx_snap.withdrawal_enabled,
                None,
            )

        dom_net, verdict, fx_net = choose(dom_nets, fx_nets)
        dep_dom = dom_net.deposit if dom_net else dom_snap.deposit_enabled
        wd_dom = dom_net.withdrawal if dom_net else dom_snap.withdrawal_enabled
        net_name = dom_net.name if dom_net else None

        if verdict is Verdict.MATCHED and fx_net is not None:
            return dep_dom, wd_dom, fx_net.deposit, fx_net.withdrawal, net_name
        if verdict is Verdict.ABSENT:
            # 해외가 이 망을 아예 취급하지 않는다 — 옮길 길이 없다.
            return dep_dom, wd_dom, False, False, net_name
        # UNKNOWN. 해외 망 정보 자체가 없으면 예전 코인 단위 값으로 두고,
        # 있는데 못 맞춘 것이면 **모른다고 말한다** — 여기서 코인 단위로
        # 접으면 방금 걷어낸 낙관 편향이 그대로 돌아온다.
        if not fx_nets:
            return dep_dom, wd_dom, fx_snap.deposit_enabled, fx_snap.withdrawal_enabled, net_name
        return dep_dom, wd_dom, None, None, net_name

    def _build_row(
        self,
        base: str,
        dom_snap: AnySnapshot,
        fx_snap: AnySnapshot,
        rate: AnyRate,
        stale_after: float,
    ) -> SpreadRow:
        """페어 하나의 행을 만든다. 호가가 비어 있으면 status=fail."""
        age = _age_seconds(dom_snap.updated_at, fx_snap.updated_at)
        dep_dom, wd_dom, dep_fx, wd_fx, net_dom = self._transfer_state(
            dom_snap, fx_snap
        )

        dom_bid = _top_level(dom_snap.bids)
        dom_ask = _top_level(dom_snap.asks)
        fx_bid = _top_level(fx_snap.bids)
        fx_ask = _top_level(fx_snap.asks)

        if not all((dom_bid, dom_ask, fx_bid, fx_ask)) or fx_ask[0] <= 0:
            return SpreadRow(
                sym=base,
                dom=dom_snap.exchange,
                fx=fx_snap.exchange,
                fwd=0.0,
                rev=0.0,
                usd=0.0,
                status=FeedStatus.FAIL,
                age=age,
                liq_dom=0.0,
                liq_fx=0.0,
                # 호가를 못 써도 입출금 상태는 따로 안다 — 그대로 싣는다
                dep_dom=dep_dom,
                wd_dom=wd_dom,
                dep_fx=dep_fx,
                wd_fx=wd_fx,
                net_dom=net_dom,
            )

        # 환율은 **이 행의 국내 거래소** KRW-USDT 호가이고, 방향마다 다르다 —
        # 김프는 원화로 USDT 를 사므로 ask, 역프는 USDT 를 원화로 파므로 bid.
        rate_ask = rate_for(rate, buy_usdt=True)
        rate_bid = rate_for(rate, buy_usdt=False)

        # /premium 과 동일한 공식 — fwd: 해외 ask 로 사서 국내 bid 에 판다
        fwd = (dom_bid[0] / (fx_ask[0] * rate_ask) - 1) * 100
        # rev: 국내 ask 로 사서 해외 bid 에 판다
        rev = (fx_bid[0] * rate_bid / dom_ask[0] - 1) * 100

        # 최우선 호가 유동성 — 양쪽(매수·매도) 중 작은 쪽, USD(T) 환산.
        # 방향이 없는 표시값이라 원화를 USDT 로 바꾸는 쪽(ask) 하나로 통일한다.
        liq_dom = min(dom_bid[0] * dom_bid[1], dom_ask[0] * dom_ask[1]) / rate_ask
        liq_fx = min(fx_bid[0] * fx_bid[1], fx_ask[0] * fx_ask[1])

        return SpreadRow(
            sym=base,
            dom=dom_snap.exchange,
            fx=fx_snap.exchange,
            fwd=fwd,
            rev=rev,
            usd=fx_snap.price,
            status=FeedStatus.STALE if age >= stale_after else FeedStatus.OK,
            age=age,
            liq_dom=liq_dom,
            liq_fx=liq_fx,
            rate_ask=rate_ask,
            rate_bid=rate_bid,
            dep_dom=dep_dom,
            wd_dom=wd_dom,
            dep_fx=dep_fx,
            wd_fx=wd_fx,
            net_dom=net_dom,
        )

    async def build(self, session: AsyncSession) -> SpreadsResult:
        """모든 (국내 × 해외 × 코인) 페어의 스프레드 행을 만든다.

        Raises:
            MarketDataNotFoundError: 스냅샷이나 환율이 없는 경우.
        """
        started = time.perf_counter()

        snapshots = await snapshots_or_db(session)
        # 환율 — 행마다 그 행의 국내 거래소 KRW-USDT 호가를 쓴다.
        # 기준 거래소 값은 응답 최상위 표시용으로만 쓰므로 여기서 한 번 확보한다
        # (없으면 여기서 404 — 기준 거래소 환율조차 없으면 볼 수 있는 게 없다).
        reference_rate = await require_usdkrw_rate_or_db(
            session, settings.krw_reference_exchange
        )
        usdkrw_rates = await usdkrw_rates_or_db(session)

        # 국내(KRW)와 해외(USDT) 스냅샷으로 나눈다.
        domestic: dict[str, dict[str, AnySnapshot]] = {}
        overseas: dict[str, dict[str, AnySnapshot]] = {}
        for snap in snapshots:
            if snap.quote == settings.krw_reference_quote:
                domestic.setdefault(snap.exchange, {})[snap.base] = snap
            elif snap.quote == settings.overseas_quote:
                overseas.setdefault(snap.exchange, {})[snap.base] = snap

        if not domestic or not overseas:
            raise MarketDataNotFoundError(
                "스프레드를 계산할 스냅샷이 부족합니다 (국내 KRW / 해외 USDT). "
                "먼저 POST /refresh 로 수집하세요.",
                detail={
                    "domestic": sorted(domestic),
                    "overseas": sorted(overseas),
                },
            )

        excluded = {b.upper() for b in settings.scan_excluded_bases}
        stale_after = settings.spread_stale_seconds

        rows: list[SpreadRow] = []
        for dom_ex in sorted(domestic):
            rate = usdkrw_rates.get(dom_ex)
            if rate is None:
                # 이 국내 거래소의 USDT 호가를 한 번도 못 받았다. 다른 거래소
                # 환율로 대신 계산하면 그 거래소의 테더 프리미엄이 섞이므로
                # 아예 내보내지 않는다.
                continue
            for fx_ex in sorted(overseas):
                if fx_ex == dom_ex:
                    continue
                fx_snaps = overseas[fx_ex]
                for base, dom_snap in domestic[dom_ex].items():
                    if base in excluded:
                        continue
                    fx_snap = fx_snaps.get(base)
                    if fx_snap is None:
                        continue  # 한쪽에만 상장된 코인은 페어가 아니다
                    rows.append(
                        self._build_row(base, dom_snap, fx_snap, rate, stale_after)
                    )

        rows.sort(key=lambda r: (r.sym, r.dom, r.fx))

        return SpreadsResult(
            rate=rate_for(reference_rate, buy_usdt=True),
            rows=rows,
            data_received_at=received_at_ms(snapshots),
            fetched_at=int(time.time() * 1000),
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
        )


spread_service = SpreadService()
