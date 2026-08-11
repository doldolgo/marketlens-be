"""수집기 — 거래소 API 를 호출해 DB 를 갱신하는 유일한 곳.

``POST /refresh`` 가 이 서비스를 부른다. 그 외의 모든 조회 API 는 거래소를
직접 부르지 않고 여기서 저장한 DB 를 읽는다.

수집 대상
    1. 국내 거래소(업비트·빗썸)의 **KRW 전종목** — 현재가 + 호가 깊이
    2. 바이낸스의 USDT 마켓 중 **국내에 상장된 코인** — 현재가 + 호가 깊이
    3. 각 거래소의 **입출금 가능 여부** (업비트·바이낸스는 API 키 필요, 빗썸은 public)
    4. 업비트·빗썸의 **KRW-USDT 환율**

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
from app.models.orderbook import MarketType, OrderBook, OrderBookLevel
from app.models.refresh import (
    ExchangeRefreshStat,
    KrwRateInfo,
    RefreshFailure,
    RefreshResult,
)
from app.models.symbol import Symbol

from dataclasses import dataclass


@dataclass(slots=True)
class _RateSample:
    """수집한 환율 한 건 — DB 저장에 필요한 원본 정보까지 들고 다닌다.

    API 응답(:class:`KrwRateInfo`)은 exchange/rate 만 노출하지만,
    ``krw_rates`` 테이블에는 원본 심볼과 시세 시각도 저장해야 한다.
    """

    exchange: str
    rate: float
    native_symbol: str
    price_timestamp: int


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

        # 1단계 — 입출금 상태 · 환율 · 국내 호가를 동시에 모은다.
        wallet_task = asyncio.gather(
            *(self._wallet(eid, warnings) for eid in _WALLET_FETCHERS)
        )
        domestic_ids = domestic_exchange_ids()
        domestic_task = asyncio.gather(
            *(self._domestic_market(eid, failures) for eid in domestic_ids)
        )
        rates_task = asyncio.gather(
            *(self._krw_rate(eid, failures) for eid in domestic_ids)
        )
        wallet_results, domestic_results, rate_results = await asyncio.gather(
            wallet_task, domestic_task, rates_task
        )
        wallets: dict[str, dict[str, WalletStatus] | None] = dict(
            zip(_WALLET_FETCHERS, wallet_results, strict=True)
        )

        # 2단계 — 바이낸스. 국내에 상장된 코인만 조회한다.
        domestic_bases: set[str] = set()
        for _, books, _ in domestic_results:
            domestic_bases |= set(books)

        rates = [r for r in rate_results if r is not None]
        upbit_rate = next(
            (r.rate for r in rates if r.exchange == settings.krw_reference_exchange),
            rates[0].rate if rates else None,
        )
        binance_result = await self._binance_market(
            domestic_bases, upbit_rate, failures, warnings
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
                # 바이낸스 호가는 USDT 기준이므로 최대 금액도 USDT 로 환산한다.
                max_amount if eid != "binance" else self._usdt_amount(
                    max_amount, upbit_rate
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

        for rate in rates:
            await repository.upsert_krw_rate(
                session,
                exchange=rate.exchange,
                rate=rate.rate,
                native_symbol=rate.native_symbol,
                price_timestamp=rate.price_timestamp,
            )
        await session.commit()

        return RefreshResult(
            snapshots=stats,
            krw_rates=[
                KrwRateInfo(exchange=r.exchange, rate=r.rate) for r in rates
            ],
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

    async def _krw_rate(
        self, exchange_id: str, failures: list[RefreshFailure]
    ) -> _RateSample | None:
        """국내 거래소 하나의 KRW-USDT 환율 (마지막 체결가)."""
        exchange = get_exchange(exchange_id)
        symbol = Symbol(
            base=settings.fx_stablecoin, quote=settings.krw_reference_quote
        )
        try:
            ticker = await exchange.fetch_ticker(symbol, market_type=MarketType.SPOT)
        except MarketLensError as exc:
            failures.append(
                RefreshFailure(
                    exchange=exchange_id,
                    sym=str(symbol),
                    error_code=exc.code,
                    message=exc.message,
                )
            )
            return None
        except Exception as exc:  # noqa: BLE001
            failures.append(
                RefreshFailure(
                    exchange=exchange_id,
                    sym=str(symbol),
                    error_code="unexpected_error",
                    message=f"{type(exc).__name__}: {exc}",
                )
            )
            return None

        # 0 이하 환율이 저장되면 조회 쪽 나눗셈이 전부 무너진다. 저장하지 않는다.
        if ticker.last_price <= 0:
            failures.append(
                RefreshFailure(
                    exchange=exchange_id,
                    sym=str(symbol),
                    error_code="invalid_rate",
                    message=f"환율이 0 이하입니다: {ticker.last_price}",
                )
            )
            return None

        return _RateSample(
            exchange=exchange_id,
            rate=ticker.last_price,
            native_symbol=ticker.native_symbol,
            price_timestamp=ticker.timestamp,
        )

    async def _binance_market(
        self,
        domestic_bases: set[str],
        upbit_rate: float | None,
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
        if upbit_rate is None:
            warnings.append(
                "KRW-USDT 환율을 얻지 못해 바이낸스 호가 저장 깊이를 "
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
                            base=base,
                            error_code=exc.code,
                            message=exc.message,
                        )
                    )
                    return base, None
                except Exception as exc:  # noqa: BLE001
                    failures.append(
                        RefreshFailure(
                            exchange="binance",
                            base=base,
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
