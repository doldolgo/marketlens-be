"""수집기 — 거래소 API 를 호출해 DB 를 갱신하는 유일한 곳.

``POST /refresh`` 가 이 서비스를 부른다. 그 외의 모든 조회 API 는 거래소를
직접 부르지 않고 여기서 저장한 DB 를 읽는다.

수집 대상
    1. 국내 거래소(업비트·빗썸)의 **KRW 전종목** — 현재가 + 호가 깊이
    2. 바이낸스의 USDT 마켓 중 **국내에 상장된 코인** — 현재가 + 호가 깊이
    3. 각 거래소의 **입출금 가능 여부** (업비트·바이낸스는 API 키 필요, 빗썸은 public)
    4. **환율** — 하나은행 고시 USD/KRW 매매기준율 (모든 원화 환산 통일)

저장은 세 갈래다.
    - ``market_snapshots`` — 코인을 찾아 **UPSERT 만** 한다 (삭제 없음).
      실시간 스프레드 창의 데이터.
    - ``premium_archive`` — 방금 갱신한 스냅샷에서 코인·시각·김프·역프만
      뽑아 한 줄씩 추가한다. 기록/통계 창의 데이터.
    - ``platform_status`` — 플랫폼당 한 행: 마지막 수신 시각과 카운터
      (전체 업데이트 횟수 +1, 입출금 불가 관측 시 실패 횟수 +1).

호가는 전부 저장하지 않는다. 슬리피지 계산이 커버해야 하는 최대 금액
(``ORDERBOOK_MAX_AMOUNT_KRW``)에 누적 체결 가능액이 도달할 때까지의 단계만
저장한다 — 그보다 깊은 호가는 계산에 쓰일 일이 없다.
"""

from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import MarketLensError
from app.core.http import request_counts
from app.db import repository
from app.db.repository import SnapshotRow
from app.exchanges.private.wallet_status import (
    MissingApiKeyError,
    WalletStatus,
    fetch_binance_wallet_status,
    fetch_bithumb_wallet_status,
    fetch_upbit_wallet_status,
)
from app.exchanges.registry import domestic_exchange_ids, get_exchange
from app.history import hana
from app.history import service as history_service
from app.history.service import premium_from_quotes
from app.models.bulk import BulkQuote
from app.models.orderbook import MarketType, OrderBook, OrderBookLevel
from app.models.refresh import (
    ExchangeRefreshStat,
    RefreshFailure,
    RefreshResult,
    UsdKrwRateInfo,
)
from app.models.symbol import Symbol


logger = logging.getLogger(__name__)

#: 거래소 ID → 입출금 상태 조회 함수
_WALLET_FETCHERS = {
    "upbit": fetch_upbit_wallet_status,
    "bithumb": fetch_bithumb_wallet_status,
    "binance": fetch_binance_wallet_status,
}


async def _completed(value: list) -> list:
    """이미 가진 값을 await 가능한 형태로 감싼다 (캐시 경로용)."""
    return value


def _truncate(levels: list[OrderBookLevel], max_amount: float) -> list[list[float]]:
    """누적 체결 가능액이 max_amount 에 도달할 때까지의 단계만 남긴다.

    max_amount 는 호가와 같은 통화 기준이다 (KRW 마켓이면 원, USDT 마켓이면 USDT).
    """
    out: list[list[float]] = []
    cumulative = 0.0
    for lv in levels:
        out.append([lv.price, lv.size])
        cumulative += lv.price * lv.size
        if cumulative >= max_amount:
            break
    return out


class CollectorService:
    """거래소 → DB 단방향 수집."""

    def __init__(self) -> None:
        # 동시 refresh 를 직렬화한다. 두 수집이 같은 행을 서로 다른 순서로
        # UPSERT/DELETE 하면 PostgreSQL 에서 락 대기·교착이 날 수 있다.
        # (프로세스 안에서만 유효 — 다중 워커 배포 시에는 스케줄러 한 곳에서만
        # refresh 를 부르는 운영 규칙이 필요하다)
        self._refresh_lock = asyncio.Lock()
        # 저빈도 작업의 마지막 실행 시각 (time.monotonic 기준).
        # 라이브 수집은 1초마다 돌지만 아래 둘은 그 주기로 돌 이유가 없다.
        self._last_archive_ts: float = 0.0
        self._last_wallet_ts: float = 0.0
        #: 거래소 → 입출금 상태. wallet_refresh_seconds 주기로만 갱신한다.
        self._wallet_cache: dict[str, dict[str, WalletStatus] | None] = {}

    async def refresh(self, session: AsyncSession) -> RefreshResult:
        """모든 수집 대상을 가져와 DB 를 갱신한다. 동시 호출은 직렬화된다."""
        async with self._refresh_lock:
            return await self._refresh(session)

    async def _refresh(self, session: AsyncSession) -> RefreshResult:
        started = time.perf_counter()
        calls_before = sum(request_counts().values())

        warnings: list[str] = []
        failures: list[RefreshFailure] = []

        # 1단계 — 입출금 상태 · 환율(하나은행) · 국내 호가를 동시에 모은다.
        #
        # 입출금 상태는 분 단위로도 잘 안 바뀌는 값인데 조회는 비싸다(업비트·
        # 바이낸스 모두 인증 호출). 1초 사이클마다 부르면 그 자체로 rate limit
        # 을 갉아먹으므로 wallet_refresh_seconds 주기로만 새로 받고, 나머지
        # 사이클은 캐시를 읽는다.
        cycle_ts = time.monotonic()
        refresh_wallet = (
            cycle_ts - self._last_wallet_ts >= settings.wallet_refresh_seconds
            or not self._wallet_cache
        )
        wallet_task = (
            asyncio.gather(*(self._wallet(eid, warnings) for eid in _WALLET_FETCHERS))
            if refresh_wallet
            else _completed([self._wallet_cache.get(eid) for eid in _WALLET_FETCHERS])
        )

        domestic_ids = domestic_exchange_ids()
        domestic_task = asyncio.gather(
            *(self._domestic_market(eid, failures) for eid in domestic_ids)
        )
        wallet_results, domestic_results, usdkrw_observation = await asyncio.gather(
            wallet_task, domestic_task, self._usdkrw_rate(failures)
        )
        wallets: dict[str, dict[str, WalletStatus] | None] = dict(
            zip(_WALLET_FETCHERS, wallet_results, strict=True)
        )
        if refresh_wallet:
            self._wallet_cache = wallets
            self._last_wallet_ts = cycle_ts

        # 이번에 환율을 못 받았으면 DB 의 마지막 값으로 계산을 이어간다.
        # (환율은 분 단위로 급변하지 않으므로 낡은 값이 없는 것보다 낫다)
        usdkrw_rate_value: float | None = (
            float(usdkrw_observation.rate) if usdkrw_observation is not None else None
        )
        if usdkrw_rate_value is None:
            stored_usdkrw = await repository.get_usdkrw_rate(session)
            if stored_usdkrw is not None and stored_usdkrw.rate > 0:
                usdkrw_rate_value = stored_usdkrw.rate
                warnings.append(
                    "환율 수집에 실패해 DB 의 마지막 환율로 계산했습니다 "
                    "(failures 참고)."
                )

        # 2단계 — 바이낸스. 국내에 상장된 코인만 조회한다.
        domestic_bases: set[str] = set()
        for _, books, _ in domestic_results:
            domestic_bases |= set(books)

        binance_result, futures_market_count = await asyncio.gather(
            self._binance_market(domestic_bases, failures),
            self._binance_futures_count(warnings),
        )
        binance_tops = binance_result[1]

        # 3단계 — DB 행 조립. 아직 저장하지 않는다.
        #
        # 조회는 벌크(거래소당 한 자릿수 호출)로 끝내고, **저장만 코인 단위로**
        # 쪼갠다. 조회까지 코인별로 쪼개면 같은 데이터를 받자고 호출이 수십 배로
        # 늘 뿐이고(빗썸 11회 → 618회), 코인 사이에 시각이 벌어져 김프가 서로
        # 다른 순간을 비교하게 된다.
        stats: list[ExchangeRefreshStat] = []
        max_amount = settings.orderbook_max_amount_krw
        now_ts = int(time.time())

        #: 거래소 → {코인: 저장할 행}
        rows_by_exchange: dict[str, dict[str, SnapshotRow]] = {}
        #: 거래소 → 이번 회차 상장 마켓 수 (platform_status 용)
        listed_count: dict[str, int] = {}

        # 세 거래소 모두 전종목 일괄 조회다 — 심볼별로 도는 경로는 남아 있지 않다.
        for eid, books, lasts in [*domestic_results, binance_result]:
            # 수집이 통째로 실패한 거래소는 DB 를 건드리지 않는다.
            # 기존 스냅샷이 남아 있고, 신선도는 updated_at 으로 판별한다.
            if not books:
                warnings.append(
                    f"{eid} 수집 결과가 비어 있어 기존 스냅샷을 유지합니다 "
                    "(failures 참고)."
                )
                stats.append(
                    ExchangeRefreshStat(
                        exchange=eid,
                        saved=0,
                        wallet_status_available=wallets.get(eid) is not None,
                    )
                )
                continue

            wallet = wallets.get(eid)
            if eid == "binance":
                # bookTicker 는 1단계뿐이라 금액 기준으로 자를 깊이가 없다.
                rows = self._tops_to_rows(eid, books, lasts, wallet, now_ts)
            else:
                rows = self._to_rows(eid, books, lasts, wallet, max_amount)
            rows_by_exchange[eid] = {r.base: r for r in rows}
            # 국내는 KRW 전종목 = 상장 현물 마켓 수. 바이낸스는 일괄 조회가
            # 준 USDT 전종목 수가 상장 마켓 수다 (호가는 교집합만 받는다).
            listed_count[eid] = len(lasts) if eid == "binance" else len(books)
            stats.append(
                ExchangeRefreshStat(
                    exchange=eid,
                    saved=len(rows),
                    wallet_status_available=wallet is not None,
                )
            )

        # 3.5단계 — 깊이 선별 조회.
        #
        # 바이낸스 일괄 조회는 최우선 1단계만 준다. 슬리피지 계산이 필요한
        # 코인 — 김프가 벌어졌고 실제로 옮길 수 있는 코인 — 에 한해서만
        # 심볼별 depth 를 추가로 받아 그 행의 호가를 덮어쓴다.
        # 평상시 대상은 0~2개이므로 대부분의 사이클은 이 단계를 건너뛴다.
        depth_targets = self._select_depth_targets(
            {eid: rows_by_exchange.get(eid, {}) for eid in domestic_ids},
            binance_tops,
            usdkrw_rate_value,
            wallets.get("binance"),
        )
        logger.info(
            "깊이 조회 대상 %d개: %s", len(depth_targets), depth_targets or "-"
        )
        if depth_targets:
            await self._apply_depth(
                depth_targets,
                rows_by_exchange.get("binance", {}),
                usdkrw_rate_value,
                failures,
            )

        # 4단계 — 교집합 / 합집합.
        #
        # 김프·역프는 (국내 거래소 × 해외 거래소) 짝이 있어야 계산된다. 그래서
        # **국내 어느 한 곳 이상 ∩ 해외 어느 한 곳 이상**을 갱신 대상으로 본다.
        # 세 거래소 모두에 있는 코인만 고르면(엄격한 3중 교집합) 업비트+바이낸스
        # 에만 있는 코인처럼 계산이 되는 것까지 버리게 된다.
        domestic_union: set[str] = set()
        for eid in domestic_ids:
            domestic_union |= set(rows_by_exchange.get(eid, {}))
        overseas_union: set[str] = set(rows_by_exchange.get("binance", {}))

        intersection = domestic_union & overseas_union

        # 5단계 — 짝을 잃은 코인 정리.
        #
        # 합집합에는 있지만 교집합에는 없는 코인 = 한쪽 시장에만 남은 코인이다.
        # 김프를 계산할 수 없어 실시간 창에 띄울 수 없으므로, **DB 에 남아 있던
        # 마지막 값으로 김프를 한 번 계산해 아카이브한 뒤** 스냅샷을 지운다.
        #
        # 합집합을 이번 회차 조회 결과로만 만들면 빠뜨리는 경우가 있다. 어제
        # 업비트+바이낸스에 있다가 오늘 업비트에서 상폐된 코인은 국내 목록에도
        # 없고, 바이낸스 호가도 (국내 상장분만 받으므로) 안 받아온다 — 이번
        # 회차 어느 집합에도 안 잡히는데 DB 에는 남아 있다. 그래서 합집합의
        # 실질은 **DB 에 행이 있는 코인 전체**로 잡는다.
        stored_bases = await repository.list_snapshot_bases(session)
        orphans = sorted(stored_bases - intersection)

        # premium_archive 는 라이브 수집보다 훨씬 느리게 적재한다.
        # 1초마다 쌓으면 (국내 2 × 코인 245)로 하루 4,400만 행이 되어 DB 가
        # 버티지 못한다. archive_interval_seconds 마다 한 번만 남긴다.
        do_archive = (
            cycle_ts - self._last_archive_ts >= settings.archive_interval_seconds
        )
        if do_archive:
            self._last_archive_ts = cycle_ts

        archived = 0
        deleted = 0
        for base in orphans:
            # 짝을 잃은 코인의 마지막 김프는 이 기회를 놓치면 영영 못 남긴다.
            # 그래도 주기 가드를 따른다 — 스냅샷 삭제는 아래에서 항상 수행되고,
            # 이 코인은 이미 실시간 창에서 빠진 상태다.
            if do_archive:
                archived += await self._archive_stored(
                    session, base, usdkrw_rate_value, now_ts
                )
            deleted += await repository.delete_snapshots_by_base(session, base)
            await session.commit()  # ← 코인 하나가 끝날 때마다 즉시 반영

        # 6단계 — 교집합 갱신. **코인 하나마다 저장하고 커밋한다.**
        #
        # 전부 모아 마지막에 한 번 커밋하면 사이클이 끝날 때까지 조회 API 가
        # 옛 값을 본다. 코인 단위로 끊으면 처리된 코인부터 곧바로 최신이 된다.
        if do_archive and usdkrw_rate_value is None:
            warnings.append(
                "환율이 없어 이번 회차의 김프 기록(premium_archive)을 건너뜁니다."
            )

        saved_by_exchange: dict[str, int] = {eid: 0 for eid in rows_by_exchange}
        dw_failed_by_exchange: dict[str, bool] = {
            eid: False for eid in rows_by_exchange
        }

        for base in sorted(intersection):
            coin_rows = [
                rows_by_exchange[eid][base]
                for eid in rows_by_exchange
                if base in rows_by_exchange[eid]
            ]
            if not coin_rows:
                continue

            # 있으면 갱신, 없으면 새로 추가 (UPSERT).
            await repository.upsert_snapshots(session, coin_rows)
            for r in coin_rows:
                saved_by_exchange[r.exchange] += 1
                if not (r.deposit_enabled and r.withdrawal_enabled):
                    dw_failed_by_exchange[r.exchange] = True

            # 김프/역프 기록 — (국내 × 해외) 짝마다 한 줄.
            if do_archive and usdkrw_rate_value is not None:
                archived += await self._archive_rows(
                    session, base, coin_rows, usdkrw_rate_value, now_ts
                )

            await session.commit()  # ← 코인 하나가 끝날 때마다 즉시 반영

        # 7단계 — 거래소 단위 마무리 (플랫폼 상태 · 환율).
        for eid in rows_by_exchange:
            await repository.bump_platform_status(
                session,
                exchange=eid,
                received_ts=now_ts,
                spot_market_count=listed_count.get(eid, 0),
                futures_market_count=(
                    futures_market_count if eid == "binance" else 0
                ),
                dw_failed=dw_failed_by_exchange[eid],
            )

        if usdkrw_observation is not None:
            # 라이브 환율 단일 행(usdkrw_rate) 갱신 — 역행은 UPSERT 가드가 막는다.
            await history_service.record_usdkrw_observation(session, usdkrw_observation)

        await session.commit()

        for stat in stats:
            stat.saved = saved_by_exchange.get(stat.exchange, 0)

        return RefreshResult(
            snapshots=stats,
            deleted=deleted,
            usdkrw=(
                UsdKrwRateInfo(
                    rate=float(usdkrw_observation.rate),
                    source_time=usdkrw_observation.ts,
                    round_no=usdkrw_observation.round_no,
                )
                if usdkrw_observation is not None
                else None
            ),
            total_saved=sum(s.saved for s in stats),
            archived=archived,
            failures=failures,
            warnings=warnings,
            total_calls=sum(request_counts().values()) - calls_before,
            fetched_at=int(time.time() * 1000),
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
        )

    # ------------------------------------------------------------------
    # 수집 단계별 구현
    # ------------------------------------------------------------------

    async def _archive_rows(
        self,
        session: AsyncSession,
        base: str,
        coin_rows: list[SnapshotRow],
        rate: float,
        now_ts: int,
    ) -> int:
        """방금 만든 행들로 (국내 × 해외) 짝마다 김프/역프를 기록한다."""
        overseas = [r for r in coin_rows if r.exchange not in domestic_exchange_ids()]
        archive_rows = [
            {
                "dom": dom.exchange,
                "fx": fx.exchange,
                "base": base,
                "ts": now_ts,
                "fwd": premium[0],
                "rev": premium[1],
            }
            for dom in coin_rows
            if dom.exchange in domestic_exchange_ids() and dom.bids and dom.asks
            for fx in overseas
            if fx.bids
            and fx.asks
            and (
                premium := premium_from_quotes(
                    dom.bids[0][0], dom.asks[0][0], fx.bids[0][0], fx.asks[0][0], rate
                )
            )
            is not None
        ]
        if not archive_rows:
            return 0
        return await repository.add_premium_rows(session, archive_rows)

    async def _archive_stored(
        self,
        session: AsyncSession,
        base: str,
        rate: float | None,
        now_ts: int,
    ) -> int:
        """지우기 직전, **DB 에 남아 있는 마지막 값**으로 김프를 한 줄 남긴다.

        짝을 잃어 삭제되는 코인이라도 직전까지는 양쪽 스냅샷이 남아 있어
        김프를 계산할 수 있다. 그 마지막 값을 기록으로 남기고 지운다 —
        기록/통계 창에서 상장폐지·페어 소멸 시점이 끊기지 않게 하려는 것이다.
        """
        if rate is None:
            return 0
        snaps = await repository.get_snapshots(session, base=base)
        rows = [
            SnapshotRow(
                exchange=s.exchange,
                base=s.base,
                native_symbol=s.native_symbol,
                quote=s.quote,
                price=s.price,
                asks=s.asks or [],
                bids=s.bids or [],
                deposit_enabled=s.deposit_enabled,
                withdrawal_enabled=s.withdrawal_enabled,
                price_timestamp=s.price_timestamp,
            )
            for s in snaps
        ]
        return await self._archive_rows(session, base, rows, rate, now_ts)

    async def _wallet(
        self, exchange_id: str, warnings: list[str]
    ) -> dict[str, WalletStatus] | None:
        """한 거래소의 입출금 상태. 실패해도 수집 전체는 계속한다."""
        try:
            return await _WALLET_FETCHERS[exchange_id]()
        except MissingApiKeyError as exc:
            warnings.append(
                f"{exchange_id} 입출금 상태를 건너뜀 — {exc} "
                "(해당 거래소의 deposit_enabled / withdrawal_enabled 는 null)"
            )
        except Exception as exc:  # noqa: BLE001 — 어떤 실패든 수집은 계속돼야 한다
            warnings.append(
                f"{exchange_id} 입출금 상태 조회 실패 — {exc} "
                "(해당 거래소의 deposit_enabled / withdrawal_enabled 는 null)"
            )
        return None

    async def _domestic_market(
        self, exchange_id: str, failures: list[RefreshFailure]
    ) -> tuple[str, dict[str, OrderBook], dict[str, float]]:
        """국내 거래소 하나의 KRW 전종목 호가 + 현재가."""
        exchange = get_exchange(exchange_id)
        try:
            books, quotes = await asyncio.gather(
                exchange.fetch_bulk_orderbooks(
                    settings.krw_reference_quote, depth=30
                ),
                exchange.fetch_bulk_quotes(
                    settings.krw_reference_quote, need_book=False
                ),
            )
        except MarketLensError as exc:
            failures.append(
                RefreshFailure(
                    exchange=exchange_id, error_code=exc.code, message=exc.message
                )
            )
            return exchange_id, {}, {}
        except Exception as exc:  # noqa: BLE001 — 파싱 오류 등도 수집 전체를 죽이면 안 된다
            failures.append(
                RefreshFailure(
                    exchange=exchange_id,
                    error_code="unexpected_error",
                    message=f"{type(exc).__name__}: {exc}",
                )
            )
            return exchange_id, {}, {}

        lasts = {b: q.last for b, q in quotes.items() if q.last is not None}
        return exchange_id, books, lasts

    async def _usdkrw_rate(
        self, failures: list[RefreshFailure]
    ) -> hana.UsdKrwObservation | None:
        """하나은행 최신 고시 USD/KRW 매매기준율.

        예전에는 국내 거래소별 KRW-USDT 시세를 환율로 썼지만, 그 값에는
        테더 프리미엄이 섞여 있다. 지금은 은행 고시 환율 하나로 통일한다.
        (0 이하 값은 hana 모듈이 파싱 단계에서 걸러 예외로 만든다)
        """
        try:
            return await hana.fetch_latest()
        except Exception as exc:  # noqa: BLE001 — 환율 실패가 수집 전체를 죽이면 안 된다
            failures.append(
                RefreshFailure(
                    exchange="hana",
                    sym="USD/KRW",
                    error_code="usdkrw_fetch_failed",
                    message=f"{type(exc).__name__}: {exc}",
                )
            )
            return None

    async def _binance_market(
        self,
        domestic_bases: set[str],
        failures: list[RefreshFailure],
    ) -> tuple[str, dict[str, BulkQuote], dict[str, float]]:
        """바이낸스 USDT 마켓 — 전종목 일괄 조회 2회로 끝낸다.

        체결가(``ticker/price``)와 최우선 호가(``ticker/bookTicker``)를 각각
        1회씩 부른다. 심볼별 ``depth`` 조회는 코인 308개면 1,540 weight 인데,
        이 두 번은 합쳐서 8 weight 다. 대신 받는 호가는 1단계뿐이라 슬리피지용
        깊이는 없다 — 최우선 호가만 쓰는 ``/spreads`` 에는 영향이 없다.
        """
        exchange = get_exchange("binance")
        try:
            prices, tops = await asyncio.gather(
                exchange.fetch_bulk_quotes(settings.overseas_quote, need_book=False),
                exchange.fetch_bulk_quotes(settings.overseas_quote, need_book=True),
            )
        except MarketLensError as exc:
            failures.append(
                RefreshFailure(
                    exchange="binance", error_code=exc.code, message=exc.message
                )
            )
            return "binance", {}, {}
        except Exception as exc:  # noqa: BLE001
            failures.append(
                RefreshFailure(
                    exchange="binance",
                    error_code="unexpected_error",
                    message=f"{type(exc).__name__}: {exc}",
                )
            )
            return "binance", {}, {}

        lasts = {b: q.last for b, q in prices.items() if q.last is not None}
        # 저장은 국내에 상장된 코인만 — 김프 계산에 짝이 없는 코인은 쓸모가 없다.
        # (lasts 는 좁히지 않는다. 상장 마켓 수 집계에 전종목 수가 필요하다)
        books = {b: q for b, q in tops.items() if b in domestic_bases}
        return "binance", books, lasts

    def _select_depth_targets(
        self,
        domestic_rows: dict[str, dict[str, SnapshotRow]],
        tops: dict[str, BulkQuote],
        rate: float | None,
        wallet_binance: dict[str, WalletStatus] | None,
    ) -> list[str]:
        """김프가 벌어졌고 실제로 옮길 수 있는 코인만 고른다.

        김프 공식은 spread_service.py 의 fwd 와 동일하게 맞춘다:
            fwd = (국내_bid / (해외_ask * 환율) - 1) * 100

        입출금이 막힌 코인은 제외한다 — 옮길 수 없으면 슬리피지를 계산할 의미가
        없고, 실제로 김프가 크게 벌어진 코인은 대부분 입금이 막혀서 벌어진다.
        """
        if rate is None or rate <= 0:
            return []

        cands: list[tuple[float, str]] = []
        for base, q in tops.items():
            if q.ask is None or q.ask <= 0:
                continue
            # 국내 여러 거래소 중 가장 높은 매수호가를 쓴다 (가장 유리한 매도처)
            dom_bid = max(
                (
                    r.bids[0][0]
                    for r in (rows.get(base) for rows in domestic_rows.values())
                    if r and r.bids
                ),
                default=None,
            )
            if dom_bid is None:
                continue

            fwd = (dom_bid / (q.ask * rate) - 1) * 100
            if fwd < settings.depth_watch_min_percent:
                continue

            # 해외 출금 + 국내 입금이 둘 다 되어야 실제로 옮길 수 있다
            fx_status = wallet_binance.get(base) if wallet_binance else None
            if not (fx_status and fx_status.withdrawal):
                continue
            dom_ok = any(
                (r is not None and r.deposit_enabled)
                for r in (rows.get(base) for rows in domestic_rows.values())
            )
            if not dom_ok:
                continue

            cands.append((fwd, base))

        cands.sort(reverse=True)
        return [b for _, b in cands[: settings.depth_watch_max_count]]

    async def _apply_depth(
        self,
        bases: list[str],
        binance_rows: dict[str, SnapshotRow],
        usdkrw_rate: float | None,
        failures: list[RefreshFailure],
    ) -> int:
        """선정된 코인의 바이낸스 깊이를 받아 1단계 호가를 덮어쓴다.

        개별 실패는 기록만 하고 수집 전체는 계속한다 — 깊이는 부가 정보라
        하나 못 받았다고 이번 회차를 통째로 버릴 이유가 없다.
        """
        exchange = get_exchange("binance")
        # 바이낸스 호가는 USDT 기준이므로 자르는 기준 금액도 환산한다.
        max_amount = self._usdt_amount(settings.orderbook_max_amount_krw, usdkrw_rate)

        async def one(base: str) -> tuple[str, OrderBook] | None:
            try:
                book = await exchange.fetch_orderbook(
                    Symbol(base=base, quote=settings.overseas_quote),
                    depth=settings.binance_orderbook_depth,
                    market_type=MarketType.SPOT,
                )
            except MarketLensError as exc:
                failures.append(
                    RefreshFailure(
                        exchange="binance",
                        sym=base,
                        error_code=exc.code,
                        message=exc.message,
                    )
                )
                return None
            except Exception as exc:  # noqa: BLE001 — 깊이 실패가 수집을 죽이면 안 된다
                failures.append(
                    RefreshFailure(
                        exchange="binance",
                        sym=base,
                        error_code="unexpected_error",
                        message=f"{type(exc).__name__}: {exc}",
                    )
                )
                return None
            return base, book

        applied = 0
        for result in await asyncio.gather(*(one(b) for b in bases)):
            if result is None:
                continue
            base, book = result
            row = binance_rows.get(base)
            # 빈 호가로 덮어쓰면 최우선 호가마저 잃는다 — 받은 게 있을 때만 교체.
            if row is None or not book.asks or not book.bids:
                continue
            row.asks = _truncate(book.asks, max_amount)
            row.bids = _truncate(book.bids, max_amount)
            applied += 1
        return applied

    async def _binance_futures_count(self, warnings: list[str]) -> int | None:
        """바이낸스 USDT 선물의 상장 마켓 수 (일괄 조회 1회).

        platform_status.futures_market_count 용이다. 실패해도 수집 전체는
        계속한다 — 이번 회차만 이전 값이 유지되도록 None 을 돌려준다.
        """
        try:
            quotes = await get_exchange("binance").fetch_bulk_quotes(
                settings.overseas_quote,
                need_book=False,
                market_type=MarketType.FUTURES,
            )
            return len(quotes)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"바이낸스 선물 마켓 수 조회 실패 — {exc}")
            return None

    # ------------------------------------------------------------------
    # 변환
    # ------------------------------------------------------------------

    def _usdt_amount(self, amount_krw: float, rate: float | None) -> float:
        """원화 최대 금액을 USDT 로 환산한다. 환율이 없으면 자르지 않는다."""
        if rate is None or rate <= 0:
            return float("inf")
        return amount_krw / rate

    def _to_rows(
        self,
        exchange_id: str,
        books: dict[str, OrderBook],
        lasts: dict[str, float],
        wallet: dict[str, WalletStatus] | None,
        max_amount: float,
    ) -> list[SnapshotRow]:
        """호가창 + 현재가 + 입출금 상태를 DB 행으로 조립한다."""
        rows: list[SnapshotRow] = []
        for base, book in books.items():
            # 현재가는 마지막 체결가. 없으면(신규 상장 직후 등) 호가 중간값으로 대체.
            price = lasts.get(base) or book.mid_price
            if price is None or price <= 0:
                continue

            status = wallet.get(base) if wallet else None
            rows.append(
                SnapshotRow(
                    exchange=exchange_id,
                    base=base,
                    native_symbol=book.native_symbol,
                    quote=book.quote,
                    price=price,
                    asks=_truncate(book.asks, max_amount),
                    bids=_truncate(book.bids, max_amount),
                    # 확인 불가(키 없음/장애)는 null 이 아니라 보수적으로 False.
                    deposit_enabled=bool(status.deposit) if status else False,
                    withdrawal_enabled=bool(status.withdrawal) if status else False,
                    price_timestamp=book.timestamp,
                )
            )
        return rows

    def _tops_to_rows(
        self,
        exchange_id: str,
        tops: dict[str, BulkQuote],
        lasts: dict[str, float],
        wallet: dict[str, WalletStatus] | None,
        now_ts: int,
    ) -> list[SnapshotRow]:
        """최우선 호가 일괄 조회 결과를 DB 행으로 조립한다.

        호가가 1단계뿐이라 _truncate 를 타지 않는다 (자를 것이 없다).
        bookTicker 에는 체결 시각이 없으므로 수집 시각을 쓴다.
        """
        rows: list[SnapshotRow] = []
        for base, q in tops.items():
            if q.bid is None or q.ask is None or q.bid <= 0 or q.ask <= 0:
                continue
            price = lasts.get(base) or q.mid
            if price is None or price <= 0:
                continue

            status = wallet.get(base) if wallet else None
            rows.append(
                SnapshotRow(
                    exchange=exchange_id,
                    base=base,
                    native_symbol=q.native_symbol,
                    quote=q.quote,
                    price=price,
                    asks=[[q.ask, q.ask_size or 0.0]],
                    bids=[[q.bid, q.bid_size or 0.0]],
                    # 확인 불가(키 없음/장애)는 null 이 아니라 보수적으로 False.
                    deposit_enabled=bool(status.deposit) if status else False,
                    withdrawal_enabled=bool(status.withdrawal) if status else False,
                    # OrderBook.timestamp 와 같은 단위(epoch ms)로 맞춘다.
                    price_timestamp=now_ts * 1000,
                )
            )
        return rows


collector_service = CollectorService()
