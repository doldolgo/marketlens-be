"""금액 기준 차익거래 시뮬레이션 서비스 — DB 스냅샷 기반.

거래소를 직접 호출하지 않는다. ``POST /refresh`` 가 저장해둔
``market_snapshots`` / ``usdkrw_rate`` 를 읽어서만 계산한다.

동작 순서
    1. 대상 코인의 전 거래소 스냅샷과 환율을 DB 에서 읽는다.
    2. 모든 호가를 요청 통화(KRW 또는 USDT)로 환산한 뒤, **최우선 매도호가가
       가장 싼 곳**(매수처)과 **최우선 매수호가가 가장 비싼 곳**(매도처)을 고른다.
       프리미엄이 양수면 해외 매수 → 국내 매도, 음수(역프)면 반대 방향이 자동으로 잡힌다.
    3. 매수처의 asks 를 투입 금액만큼 훑어 **살 수 있는 코인 수량**을 구한다.
    4. 그 수량을 매도처의 bids 에 훑어 **받을 수 있는 금액**을 구한다.
    5. 두 금액의 차이가 차익이다.

환산 규칙은 다른 조회 API 와 같다 — 모든 거래소가 **통일 환율**
(하나은행 고시 USD/KRW) 하나로 환산된다.

`/premium` 과의 차이: 프리미엄은 최우선 호가 한 점만 보지만 여기서는 호가창을
실제로 소진시킨다. 금액이 커질수록 결과가 프리미엄보다 나빠진다.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import (
    InvalidRequestError,
    MarketDataNotFoundError,
    NoArbitrageOpportunityError,
    UnsupportedExchangeError,
)
from app.db import repository
from app.exchanges.registry import get_exchange
from app.models.arbitrage import (
    ArbitrageFailure,
    ArbitrageResult,
    ExecutionSide,
    VenueQuote,
)
from app.models.orderbook import OrderBook, OrderBookLevel
from app.models.premium import PremiumDirection
from app.services.live_store import (
    AnySnapshot,
    received_at_ms,
    require_usdkrw_rate_or_db,
    snapshots_or_db,
)
from app.services.orderbook_walk import WalkResult, walk_by_amount, walk_by_quantity

#: 시뮬레이션에 쓸 호가 깊이 기본값. DB 에는 누적 체결 가능액이
#: ``ORDERBOOK_MAX_AMOUNT_KRW`` 를 커버할 때까지의 단계만 저장돼 있다.
DEFAULT_DEPTH = 100

#: 투입 금액으로 받을 수 있는 통화. 호가 환산 기준 통화이기도 하다.
SUPPORTED_INPUT_CURRENCIES = frozenset({"KRW", "USDT"})

#: 방향을 사람이 읽는 형태로.
FLOW_LABEL = {
    PremiumDirection.FWD: "해외 매수 → 국내 매도",
    PremiumDirection.REV: "국내 매수 → 해외 매도",
}


def _epoch_ms(dt: datetime | None) -> int | None:
    return int(dt.timestamp() * 1000) if dt is not None else None


def _exchange_name(exchange_id: str) -> str:
    """거래소 표시 이름. 레지스트리 메타데이터만 읽는다 (API 호출 없음)."""
    try:
        return get_exchange(exchange_id).name
    except UnsupportedExchangeError:
        return exchange_id


def _convert_levels(
    levels: list[OrderBookLevel], factor: float
) -> list[OrderBookLevel]:
    """호가 가격을 요청 통화로 환산한 사본을 만든다."""
    if factor == 1.0:
        return levels
    return [OrderBookLevel(price=lv.price * factor, size=lv.size) for lv in levels]


def _fmt_amount(amount: float, currency: str) -> str:
    """경고 메시지용 금액 표기. KRW 는 원 단위, 그 외는 소수 둘째 자리까지."""
    if currency == "KRW":
        return f"{amount:,.0f}원"
    return f"{amount:,.2f} {currency}"


@dataclass(slots=True)
class _Venue:
    """비교 후보 한 곳 — 스냅샷과 요청 통화로 환산된 호가."""

    snap: AnySnapshot
    #: depth 만큼 자른 호가 (거래소 원래 통화)
    book: OrderBook
    #: 요청 통화로 환산된 매도 호가 (오름차순)
    asks: list[OrderBookLevel]
    #: 요청 통화로 환산된 매수 호가 (내림차순)
    bids: list[OrderBookLevel]
    #: 원래 통화 → 요청 통화 계수
    to_currency: float
    #: 원래 통화 → KRW 계수 (응답의 *_krw 필드용)
    to_krw: float
    quote: VenueQuote

    @property
    def best_ask(self) -> float:
        """최우선 매도호가 (요청 통화)."""
        return self.asks[0].price

    @property
    def best_bid(self) -> float:
        """최우선 매수호가 (요청 통화)."""
        return self.bids[0].price


class ArbitrageService:
    """DB 스냅샷으로 투입 금액에 대한 실제 차익을 계산한다."""

    def _factor(self, quote: str, target: str, rate: float) -> float:
        """``quote`` 통화 가격에 곱하면 ``target`` 통화 가격이 되는 계수.

        국내(KRW) ↔ 스테이블코인(USDT) 사이만 환산한다. 환율은 통일 환율
        (하나은행 고시 USD/KRW) 하나다 — USDT≈USD 페그를 전제로 한다.
        """
        if quote == target:
            return 1.0
        if quote == settings.krw_reference_quote:
            return 1.0 / rate  # KRW → USDT
        return rate  # USDT → KRW

    def _build_venues(
        self,
        snapshots: list[AnySnapshot],
        usdkrw_rate: float,
        *,
        currency: str,
        depth: int,
        failures: list[ArbitrageFailure],
    ) -> list[_Venue]:
        """스냅샷들을 요청 통화로 환산된 비교 후보로 바꾼다.

        호가가 비어 있는 스냅샷은 후보에서 빼고 ``failures`` 에 기록한다.
        """
        venues: list[_Venue] = []
        for snap in snapshots:
            # 환산 가능한 통화(KRW/USDT)가 아니면 비교 대상이 아니다.
            if snap.quote not in (
                settings.krw_reference_quote,
                settings.overseas_quote,
            ):
                continue

            book = repository.orderbook_from_snapshot(snap, depth=depth)
            if not book.asks or not book.bids:
                failures.append(
                    ArbitrageFailure(
                        exchange=snap.exchange,
                        symbol=book.symbol,
                        error_code="market_data_not_found",
                        message=(
                            f"{snap.exchange} {snap.native_symbol} 의 저장된 "
                            "호가가 비어 있습니다."
                        ),
                    )
                )
                continue

            # 모든 거래소가 같은 통일 환율(은행 고시 USD/KRW)을 쓴다.
            to_currency = self._factor(snap.quote, currency, usdkrw_rate)
            to_krw = self._factor(snap.quote, settings.krw_reference_quote, usdkrw_rate)

            best_bid = book.bids[0].price
            best_ask = book.asks[0].price
            venues.append(
                _Venue(
                    snap=snap,
                    book=book,
                    asks=_convert_levels(book.asks, to_currency),
                    bids=_convert_levels(book.bids, to_currency),
                    to_currency=to_currency,
                    to_krw=to_krw,
                    quote=VenueQuote(
                        exchange=snap.exchange,
                        name=_exchange_name(snap.exchange),
                        best_bid_krw=best_bid * to_krw,
                        best_ask_krw=best_ask * to_krw,
                        depth_levels=min(len(book.bids), len(book.asks)),
                    ),
                )
            )
        return venues

    def _pick_venues(
        self, venues: list[_Venue], direction: PremiumDirection | None
    ) -> tuple[_Venue, _Venue]:
        """방향에 따라 (매수처, 매도처) 를 고른다.

        ``direction`` 이 ``None`` 이면 가장 싼 곳 ↔ 가장 비싼 곳을 자동으로 골라
        **항상 이득이 나는** 조합을 만든다. 방향이 지정되면 그 방향으로 고정하므로
        **손해(음수 수익률)가 나올 수 있고, 그게 정상이다.**

        국내/해외 구분은 스냅샷의 결제 통화로 한다 — KRW 마켓이면 국내,
        USDT 마켓이면 해외.
        """
        if direction is None:
            buy = min(venues, key=lambda v: v.best_ask)
            sell = max(venues, key=lambda v: v.best_bid)
            if buy.snap.exchange == sell.snap.exchange:
                raise NoArbitrageOpportunityError(
                    f"최저 매수처와 최고 매도처가 같은 거래소({buy.quote.name})입니다. "
                    "거래소 간 차익 기회가 없습니다.",
                    detail={"exchange": buy.snap.exchange},
                )
            return buy, sell

        domestic = [
            v for v in venues if v.snap.quote == settings.krw_reference_quote
        ]
        overseas = [v for v in venues if v.snap.quote == settings.overseas_quote]

        if not domestic:
            raise NoArbitrageOpportunityError(
                "국내(KRW 마켓) 스냅샷이 없어 방향을 고정한 계산을 할 수 없습니다.",
                detail={"quote": settings.krw_reference_quote},
            )
        if not overseas:
            raise NoArbitrageOpportunityError(
                "비교할 해외 거래소가 없습니다.",
                detail={"quote": settings.overseas_quote},
            )

        if direction is PremiumDirection.FWD:
            # 해외에서 가장 싸게 사서 → 국내에서 가장 비싸게 판다
            return (
                min(overseas, key=lambda v: v.best_ask),
                max(domestic, key=lambda v: v.best_bid),
            )
        # 역김프: 국내에서 가장 싸게 사서 → 해외에서 가장 비싸게 판다
        return (
            min(domestic, key=lambda v: v.best_ask),
            max(overseas, key=lambda v: v.best_bid),
        )

    def _build_side(
        self, venue: _Venue, walk: WalkResult, *, is_buy: bool
    ) -> ExecutionSide:
        """요청 통화로 훑은 결과를 응답 모델로 바꾼다.

        walk 는 요청 통화로 환산된 호가로 계산됐으므로, 거래소 원래 통화 값과
        원화 값은 계수로 되돌려서 담는다.
        """
        book = venue.book
        best_native = book.asks[0].price if is_buy else book.bids[0].price
        best_converted = best_native * venue.to_currency

        native_average = walk.average_price / venue.to_currency
        native_amount = walk.amount / venue.to_currency

        return ExecutionSide(
            exchange=venue.snap.exchange,
            name=venue.quote.name,
            average_price_krw=native_average * venue.to_krw,
            amount_krw=native_amount * venue.to_krw,
            slippage_percent=walk.slippage_percent(best_converted, is_buy=is_buy),
            levels_consumed=walk.levels_consumed,
            depth_exhausted=walk.exhausted,
            data_updated_at=_epoch_ms(venue.snap.updated_at),
        )

    async def simulate(
        self,
        session: AsyncSession,
        base: str,
        *,
        amount: float,
        currency: str = "KRW",
        exchanges: list[str] | None = None,
        direction: PremiumDirection | None = None,
        depth: int = DEFAULT_DEPTH,
    ) -> ArbitrageResult:
        """투입 금액에 대한 차익을 메모리 스냅샷(없으면 DB)으로 계산한다.

        Args:
            session: DB 세션.
            base: 대상 코인 (예: "BTC").
            amount: 투입 금액 (``currency`` 통화 기준).
            currency: 투입 금액의 통화 ("KRW" 또는 "USDT"). 모든 호가를 이
                통화로 환산해 비교하고 체결을 시뮬레이션한다.
            exchanges: 대상 거래소 ID 목록. ``direction`` 이 지정되면 **해외 거래소**
                목록으로 해석되고 국내(KRW 마켓) 거래소는 자동 포함된다.
            direction: 차익 방향을 고정한다. ``None`` 이면 가장 싼 곳 ↔ 가장 비싼 곳을
                자동으로 고른다 — 가능한 조합 중 **가장 유리한 것**일 뿐, 스프레드가
                가격차보다 크면 음수 수익이 나올 수 있다 (경고로 표시된다).
                방향을 지정하면 그 방향으로 고정하므로 **손해(음수)가 나올 수
                있고 그게 정상이다.**
            depth: 훑을 호가 단계 수 (저장된 호가 안에서).

        Raises:
            InvalidRequestError: 지원하지 않는 통화이거나 금액이 너무 작은 경우.
            MarketDataNotFoundError: 스냅샷 또는 환율이 없는 경우.
            NoArbitrageOpportunityError: 비교 가능한 거래소가 2곳 미만이거나,
                자동 선택에서 최저 매수처와 최고 매도처가 같은 거래소인 경우.
        """
        started = time.perf_counter()
        base = base.upper()

        currency = currency.upper()
        if currency not in SUPPORTED_INPUT_CURRENCIES:
            raise InvalidRequestError(
                f"지원하지 않는 투입 통화입니다: {currency}. "
                f"{' 또는 '.join(sorted(SUPPORTED_INPUT_CURRENCIES))} 만 지원합니다.",
                detail={"currency": currency},
            )

        # 잘못된 거래소 ID 는 여기서 걸러진다 (레지스트리는 ID 검증 용도로만 쓴다).
        requested = (
            [get_exchange(e).id for e in exchanges] if exchanges is not None else None
        )

        snapshots = await snapshots_or_db(session, base=base)

        if not snapshots:
            raise MarketDataNotFoundError(
                f"{base} 스냅샷이 없습니다. POST /refresh 로 데이터를 "
                "수집했는지, 상장된 코인인지 확인하세요.",
                detail={"base": base},
            )
        # 통일 환율 — 없거나 0 이하면 여기서 404 성격의 예외가 난다.
        usdkrw = await require_usdkrw_rate_or_db(session)

        # 대상 거래소 필터. 명시적으로 요청했는데 스냅샷이 없으면 실패로 기록한다.
        failures: list[ArbitrageFailure] = []
        pool = snapshots
        if requested is not None:
            available = {s.exchange for s in snapshots}
            for exchange_id in requested:
                if exchange_id not in available:
                    failures.append(
                        ArbitrageFailure(
                            exchange=exchange_id,
                            symbol=base,
                            error_code="market_data_not_found",
                            message=(
                                f"DB 에 {exchange_id} 거래소의 {base} 스냅샷이 "
                                "없습니다. POST /refresh 로 수집했는지, 상장된 "
                                "코인인지 확인하세요."
                            ),
                        )
                    )
            wanted = set(requested)
            pool = [
                s
                for s in snapshots
                if s.exchange in wanted
                # direction 지정 시 국내(KRW 마켓)는 방향의 한쪽 축이므로 항상 포함
                or (
                    direction is not None
                    and s.quote == settings.krw_reference_quote
                )
            ]

        venues = self._build_venues(
            pool,
            usdkrw.rate,
            currency=currency,
            depth=depth,
            failures=failures,
        )

        if len(venues) < 2:
            raise NoArbitrageOpportunityError(
                "비교 가능한 거래소가 2곳 미만이라 차익을 계산할 수 없습니다. "
                f"성공 {len(venues)}곳 / 실패 {len(failures)}곳",
                detail={
                    "succeeded": [v.snap.exchange for v in venues],
                    "failures": [f.model_dump() for f in failures],
                },
            )

        # 방향에 따라 매수처 / 매도처를 고른다
        buy, sell = self._pick_venues(venues, direction)
        warnings: list[str] = []

        # 매수: 요청 통화로 환산된 asks 를 투입 금액만큼 훑는다
        buy_walk = walk_by_amount(buy.asks, amount)
        if buy_walk.quantity <= 0:
            raise InvalidRequestError(
                "투입 금액이 너무 작아 최소 단위도 체결되지 않습니다.",
                detail={"input_amount": amount, "currency": currency},
            )

        # 매도: 매수한 수량을 요청 통화로 환산된 bids 에 훑는다
        sell_walk = walk_by_quantity(sell.bids, buy_walk.quantity)

        buy_side = self._build_side(buy, buy_walk, is_buy=True)
        sell_side = self._build_side(sell, sell_walk, is_buy=False)

        # 차익 — 요청 통화 기준으로 계산하고, 원화 값은 거래소별 환율로 환산해 담는다
        profit = sell_walk.amount - buy_walk.amount
        profit_percent = (profit / buy_walk.amount * 100) if buy_walk.amount else 0.0
        profit_krw = sell_side.amount_krw - buy_side.amount_krw

        # 표면 프리미엄: 최우선 호가만 본 가격차 (슬리피지 미반영)
        premium_percent = (
            (sell.best_bid / buy.best_ask - 1) * 100 if buy.best_ask else 0.0
        )
        capture = (profit_percent / premium_percent * 100) if premium_percent else 0.0

        input_krw = (
            amount
            if currency == settings.krw_reference_quote
            else amount * usdkrw.rate
        )

        # --- 경고 ---
        if buy_walk.exhausted:
            warnings.append(
                f"{buy_side.name} 매도호가 {buy_walk.levels_consumed}단계를 모두 "
                f"소진했습니다. 투입 금액 중 "
                f"{_fmt_amount(buy_walk.amount, currency)}만 체결됩니다 "
                "(저장된 호가 깊이 한계)."
            )
        if sell_walk.exhausted:
            warnings.append(
                f"{sell_side.name} 매수호가 {sell_walk.levels_consumed}단계를 모두 "
                f"소진했습니다. 매수한 {buy_walk.quantity:.8f} {base} 중 "
                "일부만 매도됩니다."
            )
        if profit < 0:
            if direction is not None:
                warnings.append(
                    f"이 방향({FLOW_LABEL[direction]})은 현재 손해입니다. "
                    "반대 방향을 확인해 보세요."
                )
            else:
                warnings.append(
                    "자동 선택된 가장 유리한 조합조차 현재는 손해입니다. "
                    "스프레드가 거래소 간 가격차보다 큰 상태로, 지금은 차익 기회가 "
                    "없습니다."
                )

        # 입출금이 막혀 있으면 코인을 옮길 수 없어 이 경로 자체가 실행 불가능하다.
        withdrawal_available = buy.snap.withdrawal_enabled
        deposit_available = sell.snap.deposit_enabled
        if withdrawal_available is False:
            warnings.append(
                f"{buy_side.name}에서 {base} 출금이 막혀 있습니다. "
                "이 경로는 현재 실행할 수 없습니다."
            )
        elif withdrawal_available is None:
            # 경고를 아예 안 내면 "경고 없음 = 괜찮음"으로 읽힌다.
            # 모르는 것은 모른다고 말한다.
            warnings.append(
                f"{buy_side.name}의 {base} 출금 가능 여부를 확인하지 "
                "못했습니다. 열려 있다고 가정하지 마십시오."
            )
        if deposit_available is False:
            warnings.append(
                f"{sell_side.name}에서 {base} 입금이 막혀 있습니다. "
                "이 경로는 현재 실행할 수 없습니다."
            )
        elif deposit_available is None:
            warnings.append(
                f"{sell_side.name}의 {base} 입금 가능 여부를 확인하지 "
                "못했습니다. 열려 있다고 가정하지 마십시오."
            )
        if input_krw > settings.orderbook_max_amount_krw:
            warnings.append(
                f"요청 금액({input_krw:,.0f}원)이 호가 저장 한도"
                f"({settings.orderbook_max_amount_krw:,.0f}원)보다 큽니다. "
                "슬리피지가 실제보다 작게 계산됐을 수 있습니다."
            )
        warnings.append(
            "거래 수수료·출금 수수료·코인 전송 시간이 반영되지 않은 이론값입니다. "
            "실제로는 전송 중 가격이 변동합니다."
        )

        # 데이터 신선도 — 비교에 쓴 스냅샷들의 DB 갱신 시각
        stamps = [v.snap.updated_at for v in venues if v.snap.updated_at is not None]

        return ArbitrageResult(
            data_received_at=received_at_ms(snapshots),
            sym=base,
            direction=direction,
            input_amount_krw=input_krw,
            usd_krw_rate=usdkrw.rate,
            premium_percent=premium_percent,
            buy=buy_side,
            sell=sell_side,
            quantity=buy_walk.quantity,
            withdrawal_available=withdrawal_available,
            deposit_available=deposit_available,
            profit_krw=profit_krw,
            profit_percent=profit_percent,
            premium_capture_percent=capture,
            candidates=[v.quote for v in sorted(venues, key=lambda v: v.best_ask)],
            failures=failures,
            warnings=warnings,
            data_oldest_at=_epoch_ms(min(stamps)) if stamps else None,
            data_newest_at=_epoch_ms(max(stamps)) if stamps else None,
            fetched_at=int(time.time() * 1000),
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
        )


arbitrage_service = ArbitrageService()
