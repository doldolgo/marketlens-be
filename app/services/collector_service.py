"""수집기 — 거래소 API 를 호출해 DB 를 갱신하는 유일한 곳.

``POST /refresh`` 가 이 서비스를 부른다. 그 외의 모든 조회 API 는 거래소를
직접 부르지 않고 여기서 저장한 DB 를 읽는다.

수집 대상
    1. 국내 거래소(업비트·빗썸)의 **KRW 전종목** — 현재가 + 호가 깊이
    2. 바이낸스의 USDT 마켓 중 **국내에 상장된 코인** — 현재가 + 호가 깊이
    3. 각 거래소의 **입출금 가능 여부** (업비트·바이낸스는 API 키 필요, 빗썸은 public)
    4. **환율** — 하나은행 고시 USD/KRW 매매기준율 (거래소별 KRW-USDT 시세를
       쓰던 예전 방식을 대체. 모든 원화 환산이 이 값 하나로 통일된다)

환율은 라이브 단일 행(``fx_rate``)과 변동 이력 스테이징(``fx_points``)에
동시에 반영된다 — refresh 만 주기적으로 돌아도 환율 이력이 쌓인다.

호가는 전부 저장하지 않는다. 슬리피지 계산이 커버해야 하는 최대 금액
(``ORDERBOOK_MAX_AMOUNT_KRW``)에 누적 체결 가능액이 도달할 때까지의 단계만
저장한다 — 그보다 깊은 호가는 계산에 쓰일 일이 없다.
"""

from __future__ import annotations

import asyncio
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
from app.models.orderbook import MarketType, OrderBook, OrderBookLevel
from app.models.refresh import (
    ExchangeRefreshStat,
    FxRateInfo,
    RefreshFailure,
    RefreshResult,
)
from app.models.symbol import Symbol


#: 거래소 ID → 입출금 상태 조회 함수
_WALLET_FETCHERS = {
    "upbit": fetch_upbit_wallet_status,
    "bithumb": fetch_bithumb_wallet_status,
    "binance": fetch_binance_wallet_status,
}


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
        wallet_task = asyncio.gather(
            *(self._wallet(eid, warnings) for eid in _WALLET_FETCHERS)
        )
        domestic_ids = domestic_exchange_ids()
        domestic_task = asyncio.gather(
            *(self._domestic_market(eid, failures) for eid in domestic_ids)
        )
        wallet_results, domestic_results, fx_observation = await asyncio.gather(
            wallet_task, domestic_task, self._fx_rate(failures)
        )
        wallets: dict[str, dict[str, WalletStatus] | None] = dict(
            zip(_WALLET_FETCHERS, wallet_results, strict=True)
        )

        # 이번에 환율을 못 받았으면 DB 의 마지막 값으로 계산을 이어간다.
        # (환율은 분 단위로 급변하지 않으므로 낡은 값이 없는 것보다 낫다)
        fx_rate_value: float | None = (
            float(fx_observation.rate) if fx_observation is not None else None
        )
        if fx_rate_value is None:
            stored_fx = await repository.get_fx_rate(session)
            if stored_fx is not None and stored_fx.rate > 0:
                fx_rate_value = stored_fx.rate
                warnings.append(
                    "환율 수집에 실패해 DB 의 마지막 환율로 계산했습니다 "
                    "(failures 참고)."
                )

        # 2단계 — 바이낸스. 국내에 상장된 코인만 조회한다.
        domestic_bases: set[str] = set()
        for _, books, _ in domestic_results:
            domestic_bases |= set(books)

        binance_result = await self._binance_market(
            domestic_bases, fx_rate_value, failures, warnings
        )

        # 3단계 — DB 반영. 한 트랜잭션으로 묶는다.
        stats: list[ExchangeRefreshStat] = []
        max_amount = settings.orderbook_max_amount_krw

        for (eid, books, lasts), mode in [
            *[(r, "bulk") for r in domestic_results],
            (binance_result, "per_symbol"),
        ]:
            # 수집이 통째로 실패한 거래소는 DB 를 건드리지 않는다.
            # 빈 결과로 replace 하면 일시적 API 장애 한 번에 그 거래소의
            # 기존 데이터가 전부 삭제되어 버린다. 낡은 데이터가 없는 것보다 낫고,
            # 신선도는 updated_at 으로 판별할 수 있다.
            if not books:
                warnings.append(
                    f"{eid} 수집 결과가 비어 있어 기존 스냅샷을 유지합니다 "
                    "(failures 참고)."
                )
                stats.append(
                    ExchangeRefreshStat(
                        exchange=eid,
                        saved=0,
                        deleted=0,
                        wallet_status_available=wallets.get(eid) is not None,
                        mode=mode,
                    )
                )
                continue

            wallet = wallets.get(eid)
            rows = self._to_rows(
                eid,
                books,
                lasts,
                wallet,
                # 바이낸스 호가는 USDT 기준이므로 최대 금액도 환산한다.
                # (USDT≈USD 로 보고 은행 환율을 쓴다 — 자르는 깊이 기준일 뿐이라
                #  1% 미만의 페그 오차는 결과에 의미 있는 차이를 만들지 않는다)
                max_amount if eid != "binance" else self._usdt_amount(
                    max_amount, fx_rate_value
                ),
            )
            saved, deleted = await repository.replace_exchange_snapshots(
                session, eid, rows
            )
            stats.append(
                ExchangeRefreshStat(
                    exchange=eid,
                    saved=saved,
                    deleted=deleted,
                    wallet_status_available=wallet is not None,
                    mode=mode,
                )
            )

        if fx_observation is not None:
            # 라이브 단일 행(fx_rate) 갱신 + 환율 변동 이력 적재.
            # "변동만 저장" 규칙은 record_fx_observation 한 곳에 구현돼 있고
            # POST /history/sync 도 같은 경로를 쓴다.
            await history_service.record_fx_observation(session, fx_observation)
        await session.commit()

        return RefreshResult(
            snapshots=stats,
            fx=(
                FxRateInfo(
                    rate=float(fx_observation.rate),
                    source_time=fx_observation.ts,
                    round_no=fx_observation.round_no,
                )
                if fx_observation is not None
                else None
            ),
            total_saved=sum(s.saved for s in stats),
            failures=failures,
            warnings=warnings,
            total_calls=sum(request_counts().values()) - calls_before,
            fetched_at=int(time.time() * 1000),
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
        )

    # ------------------------------------------------------------------
    # 수집 단계별 구현
    # ------------------------------------------------------------------

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

    async def _fx_rate(
        self, failures: list[RefreshFailure]
    ) -> hana.FxObservation | None:
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
                    exchange="fx_hana",
                    sym="USD/KRW",
                    error_code="fx_fetch_failed",
                    message=f"{type(exc).__name__}: {exc}",
                )
            )
            return None

    async def _binance_market(
        self,
        domestic_bases: set[str],
        fx_rate: float | None,
        failures: list[RefreshFailure],
        warnings: list[str],
    ) -> tuple[str, dict[str, OrderBook], dict[str, float]]:
        """바이낸스 USDT 마켓 — 국내 상장 코인만, 심볼별 depth 조회.

        바이낸스는 호가 깊이를 일괄로 주는 엔드포인트가 없어 심볼별로 부른다.
        전종목 체결가(1회 호출)로 대상을 교집합으로 좁힌 뒤 동시 실행 수를
        제한해 rate limit 을 지킨다.
        """
        exchange = get_exchange("binance")
        try:
            quotes = await exchange.fetch_bulk_quotes(
                settings.fx_stablecoin, need_book=False
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

        lasts = {b: q.last for b, q in quotes.items() if q.last is not None}
        targets = sorted(domestic_bases & set(lasts))
        if fx_rate is None:
            warnings.append(
                "USD/KRW 환율(하나은행 고시)을 얻지 못해 바이낸스 호가 저장 깊이를 "
                "금액 기준으로 자르지 못했습니다. 조회된 전체 깊이를 저장합니다."
            )

        semaphore = asyncio.Semaphore(settings.refresh_concurrency)

        async def one(base: str) -> tuple[str, OrderBook | None]:
            async with semaphore:
                try:
                    book = await exchange.fetch_orderbook(
                        Symbol(base=base, quote=settings.fx_stablecoin),
                        depth=settings.binance_orderbook_depth,
                        market_type=MarketType.SPOT,
                    )
                    return base, book
                except MarketLensError as exc:
                    failures.append(
                        RefreshFailure(
                            exchange="binance",
                            sym=base,
                            error_code=exc.code,
                            message=exc.message,
                        )
                    )
                    return base, None
                except Exception as exc:  # noqa: BLE001
                    failures.append(
                        RefreshFailure(
                            exchange="binance",
                            sym=base,
                            error_code="unexpected_error",
                            message=f"{type(exc).__name__}: {exc}",
                        )
                    )
                    return base, None

        results = await asyncio.gather(*(one(b) for b in targets))
        books = {base: book for base, book in results if book is not None}
        return "binance", books, lasts

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
                    deposit_enabled=status.deposit if status else None,
                    withdrawal_enabled=status.withdrawal if status else None,
                    price_timestamp=book.timestamp,
                )
            )
        return rows


collector_service = CollectorService()
