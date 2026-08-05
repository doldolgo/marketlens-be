"""금액 기준 차익거래 시뮬레이션 서비스.

동작 순서
    1. 대상 거래소들의 호가창(깊이 포함)과 환율을 동시에 조회한다.
    2. 모든 호가를 원화로 환산한 뒤, **최우선 매도호가가 가장 싼 곳**(매수처)과
       **최우선 매수호가가 가장 비싼 곳**(매도처)을 고른다.
       프리미엄이 양수면 해외 매수 → 국내 매도, 음수(역프)면 반대 방향이 자동으로 잡힌다.
    3. 매수처의 asks 를 투입 금액만큼 훑어 **살 수 있는 코인 수량**을 구한다.
    4. 그 수량을 매도처의 bids 에 훑어 **받을 수 있는 금액**을 구한다.
    5. 두 금액의 차이가 차익이다.

`/premium` 과의 차이: 프리미엄은 최우선 호가 한 점만 보지만 여기서는 호가창을
실제로 소진시킨다. 금액이 커질수록 결과가 프리미엄보다 나빠진다.
"""

from __future__ import annotations

import asyncio
import time

from app.core.config import settings
from app.core.errors import (
    ExchangeAPIError,
    InvalidRequestError,
    MarketLensError,
    NoArbitrageOpportunityError,
)
from app.exchanges.registry import all_exchanges, get_exchange
from app.models.arbitrage import (
    ArbitrageFailure,
    ArbitrageResult,
    ExecutionSide,
    VenueQuote,
)
from app.models.orderbook import MarketType, OrderBook, OrderBookLevel
from app.models.symbol import Symbol
from app.services.fx import FxRate, fx_service
from app.services.market_data_service import market_data_service
from app.services.orderbook_walk import WalkResult, walk_buy, walk_sell

#: 시뮬레이션에 쓸 호가 깊이. 업비트는 최대 30단계까지만 내려준다.
DEFAULT_DEPTH = 100

#: 투입 금액으로 받을 수 있는 통화.
SUPPORTED_INPUT_CURRENCIES = frozenset({"KRW", "USDT"})


class ArbitrageService:
    """투입 금액에 대한 실제 차익을 계산한다."""

    def resolve_targets(
        self,
        base: str,
        exchanges: list[str] | None,
        market_type: MarketType,
    ) -> list[tuple[str, Symbol, MarketType]]:
        """비교 대상 (거래소, 심볼, 시장구분) 목록을 만든다.

        각 거래소의 ``default_quote`` 를 쓴다 (업비트 KRW, 바이낸스 USDT).
        생략하면 해당 마켓을 지원하는 모든 거래소가 대상이다.
        """
        ids = exchanges if exchanges is not None else [e.id for e in all_exchanges()]
        targets: list[tuple[str, Symbol, MarketType]] = []

        for exchange_id in ids:
            exchange = get_exchange(exchange_id)
            symbol = Symbol(base=base.upper(), quote=exchange.default_quote)
            if exchanges is not None:
                # 명시적으로 요청했으면 지원 여부를 알려주기 위해 예외를 던진다.
                exchange.ensure_supported(symbol, market_type)
            elif not exchange.supports(symbol, market_type):
                continue
            targets.append((exchange.id, symbol, market_type))

        return targets

    def _to_krw_factor(self, quote_currency: str, fx: FxRate) -> float:
        """결제 통화 가격에 곱하면 원화가 되는 계수."""
        if quote_currency == "KRW":
            return 1.0
        if quote_currency == "USDT":
            return fx.rate
        raise ExchangeAPIError(
            f"{quote_currency} 마켓은 원화 환산을 지원하지 않습니다.",
            detail={"quote": quote_currency},
        )

    def _to_venue(self, book: OrderBook, fx: FxRate) -> VenueQuote:
        """호가창을 원화 기준 요약으로 바꾼다."""
        best_bid, best_ask, mid = book.best_bid, book.best_ask, book.mid_price
        if best_bid is None or best_ask is None or mid is None:
            raise ExchangeAPIError(
                f"{book.exchange} {book.native_symbol} 호가가 비어 있습니다.",
                detail={"exchange": book.exchange, "native_symbol": book.native_symbol},
            )

        factor = self._to_krw_factor(book.quote, fx)
        return VenueQuote(
            exchange=book.exchange,
            name=get_exchange(book.exchange).name,
            symbol=book.symbol,
            native_symbol=book.native_symbol,
            quote_currency=book.quote,
            best_bid_krw=best_bid * factor,
            best_ask_krw=best_ask * factor,
            mid_price_krw=mid * factor,
            depth_levels=min(len(book.bids), len(book.asks)),
        )

    def _krw_levels(self, levels: list[OrderBookLevel], factor: float) -> list[OrderBookLevel]:
        """호가 가격을 원화로 환산한 사본을 만든다."""
        if factor == 1.0:
            return levels
        return [OrderBookLevel(price=lv.price * factor, size=lv.size) for lv in levels]

    def _build_side(
        self,
        book: OrderBook,
        walk: WalkResult,
        fx: FxRate,
        *,
        is_buy: bool,
    ) -> ExecutionSide:
        """원화 기준으로 훑은 결과를 응답 모델로 바꾼다.

        walk 는 원화 환산 호가로 계산됐으므로, 거래소 원래 통화 값은 되돌려서 담는다.
        """
        factor = self._to_krw_factor(book.quote, fx)
        best_krw = (book.best_ask if is_buy else book.best_bid) * factor

        return ExecutionSide(
            exchange=book.exchange,
            name=get_exchange(book.exchange).name,
            symbol=book.symbol,
            native_symbol=book.native_symbol,
            quote_currency=book.quote,
            best_price=best_krw / factor,
            average_price=walk.average_price / factor,
            average_price_krw=walk.average_price,
            amount=walk.amount / factor,
            amount_krw=walk.amount,
            slippage_percent=walk.slippage_percent(best_krw, is_buy=is_buy),
            levels_consumed=walk.levels_consumed,
            depth_exhausted=walk.exhausted,
            timestamp=book.timestamp,
            latency_ms=book.latency_ms,
        )

    async def simulate(
        self,
        base: str,
        *,
        amount: float,
        currency: str = "KRW",
        exchanges: list[str] | None = None,
        market_type: MarketType = MarketType.SPOT,
        depth: int = DEFAULT_DEPTH,
    ) -> ArbitrageResult:
        """투입 금액에 대한 차익을 계산한다.

        Args:
            base: 대상 코인 (예: "BTC").
            amount: 투입 금액.
            currency: 투입 금액의 통화 ("KRW" 또는 "USDT").
            exchanges: 대상 거래소 ID 목록. 생략하면 지원되는 전체.
            market_type: 현물/선물 구분.
            depth: 훑을 호가 단계 수.

        Raises:
            InvalidRequestError: 지원하지 않는 통화이거나 금액이 너무 작은 경우.
            NoArbitrageOpportunityError: 비교 가능한 거래소가 2곳 미만이거나,
                최저 매수처와 최고 매도처가 같은 거래소인 경우.
        """
        started = time.perf_counter()

        currency = currency.upper()
        if currency not in SUPPORTED_INPUT_CURRENCIES:
            raise InvalidRequestError(
                f"지원하지 않는 투입 통화입니다: {currency}. "
                f"{' 또는 '.join(sorted(SUPPORTED_INPUT_CURRENCIES))} 만 지원합니다.",
                detail={"currency": currency},
            )

        targets = self.resolve_targets(base, exchanges, market_type)
        (books, fetch_failures), fx = await asyncio.gather(
            market_data_service.fetch_orderbooks(targets, depth=depth),
            fx_service.fetch_rate(),
        )

        failures = [
            ArbitrageFailure(
                exchange=f.exchange, symbol=f.symbol,
                error_code=f.error_code, message=f.message,
            )
            for f in fetch_failures
        ]

        venues: list[tuple[VenueQuote, OrderBook]] = []
        for book in books:
            try:
                venues.append((self._to_venue(book, fx), book))
            except MarketLensError as exc:
                failures.append(
                    ArbitrageFailure(
                        exchange=book.exchange, symbol=book.symbol,
                        error_code=exc.code, message=exc.message,
                    )
                )

        if len(venues) < 2:
            raise NoArbitrageOpportunityError(
                "비교 가능한 거래소가 2곳 미만이라 차익을 계산할 수 없습니다. "
                f"성공 {len(venues)}곳 / 실패 {len(failures)}곳",
                detail={
                    "succeeded": [v.exchange for v, _ in venues],
                    "failures": [f.model_dump() for f in failures],
                },
            )

        # 가장 싸게 살 수 있는 곳 / 가장 비싸게 팔 수 있는 곳
        buy_quote, buy_book = min(venues, key=lambda v: v[0].best_ask_krw)
        sell_quote, sell_book = max(venues, key=lambda v: v[0].best_bid_krw)

        warnings: list[str] = []
        if buy_quote.exchange == sell_quote.exchange:
            raise NoArbitrageOpportunityError(
                f"최저 매수처와 최고 매도처가 같은 거래소({buy_quote.name})입니다. "
                "거래소 간 차익 기회가 없습니다.",
                detail={"exchange": buy_quote.exchange},
            )

        # 투입 금액을 원화로 통일
        input_krw = amount if currency == "KRW" else amount * fx.rate

        # 매수: 원화 환산 asks 를 예산만큼 훑는다
        buy_factor = self._to_krw_factor(buy_book.quote, fx)
        buy_walk = walk_buy(self._krw_levels(buy_book.asks, buy_factor), input_krw)

        if buy_walk.quantity <= 0:
            raise InvalidRequestError(
                "투입 금액이 너무 작아 최소 단위도 체결되지 않습니다.",
                detail={"input_amount_krw": input_krw},
            )

        # 매도: 매수한 수량을 원화 환산 bids 에 훑는다
        sell_factor = self._to_krw_factor(sell_book.quote, fx)
        sell_walk = walk_sell(self._krw_levels(sell_book.bids, sell_factor), buy_walk.quantity)

        buy_side = self._build_side(buy_book, buy_walk, fx, is_buy=True)
        sell_side = self._build_side(sell_book, sell_walk, fx, is_buy=False)

        profit_krw = sell_walk.amount - buy_walk.amount
        profit_percent = (profit_krw / buy_walk.amount * 100) if buy_walk.amount else 0.0

        # 표면 프리미엄: 최우선 호가만 본 가격차 (슬리피지 미반영)
        premium_percent = (
            (sell_quote.best_bid_krw / buy_quote.best_ask_krw - 1) * 100
            if buy_quote.best_ask_krw else 0.0
        )
        capture = (profit_percent / premium_percent * 100) if premium_percent else 0.0

        # --- 경고 ---
        if buy_walk.exhausted:
            warnings.append(
                f"{buy_side.name} 매도호가 {buy_walk.levels_consumed}단계를 모두 소진했습니다. "
                f"투입 금액 중 {buy_walk.amount:,.0f}원만 체결됩니다 "
                "(호가창 깊이 한계)."
            )
        if sell_walk.exhausted:
            warnings.append(
                f"{sell_side.name} 매수호가 {sell_walk.levels_consumed}단계를 모두 소진했습니다. "
                f"매수한 {buy_walk.quantity:.8f} {base.upper()} 중 일부만 매도됩니다."
            )
        if buy_book.exchange == settings.krw_reference_exchange or (
            sell_book.exchange == settings.krw_reference_exchange
        ):
            warnings.append(
                "거래 수수료·출금 수수료·코인 전송 시간이 반영되지 않은 이론값입니다. "
                "실제로는 전송 중 가격이 변동합니다."
            )

        return ArbitrageResult(
            base=base.upper(),
            market_type=market_type,
            input_amount=amount,
            input_currency=currency,
            input_amount_krw=input_krw,
            usdt_krw_rate=fx.rate,
            fx_source=fx.source,
            premium_percent=premium_percent,
            buy=buy_side,
            sell=sell_side,
            quantity=buy_walk.quantity,
            profit_krw=profit_krw,
            profit_percent=profit_percent,
            premium_capture_percent=capture,
            candidates=sorted((v for v, _ in venues), key=lambda v: v.best_ask_krw),
            failures=failures,
            warnings=warnings,
            fetched_at=int(time.time() * 1000),
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
        )


arbitrage_service = ArbitrageService()
