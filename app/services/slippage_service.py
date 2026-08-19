"""슬리피지 계산 서비스 — DB 스냅샷 호가 기반.

DB 에 저장된 호가를 훑어 **최우선 호가 대비 얼마나 불리해지는지**를 계산한다.
거래소를 직접 호출하지 않는다 — ``POST /refresh`` 가 ``market_snapshots`` 에
저장해둔 호가를 읽어서만 계산한다.

`/arbitrage` 가 두 거래소를 묶어 차익을 보는 것이라면, 이쪽은 **한 거래소 한 방향**만
본다. "이 거래소에서 1억원어치 사면 슬리피지가 몇 %인가" 같은 질문에 답한다.

업비트 호가창에서 마우스를 올리면 뜨는 툴팁(평균가 · 누적량 · 누적액)과 같은 계산이며,
``fills`` 가 그 단계별 값을 그대로 담는다.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InvalidRequestError, MarketDataNotFoundError
from app.db import repository
from app.exchanges.registry import get_exchange
from app.models.orderbook import OrderBook
from app.models.slippage import FillLevel, OrderSide, SlippageResult
from app.models.symbol import Symbol
from app.services.live_store import require_snapshot_or_db
from app.services.orderbook_walk import WalkResult, walk_by_amount, walk_by_quantity

#: 기본 호가 깊이. 저장된 호가(수집 한도 내)를 사실상 전부 훑는 수준이다.
DEFAULT_DEPTH = 100


def _epoch_ms(dt: datetime | None) -> int | None:
    return int(dt.timestamp() * 1000) if dt is not None else None


class SlippageService:
    """DB 스냅샷의 호가를 훑어 슬리피지를 계산한다."""

    def _build_fills(self, walk: WalkResult) -> list[FillLevel]:
        """단계별 체결 내역을 누적값과 함께 만든다."""
        fills: list[FillLevel] = []
        cum_qty = cum_amt = 0.0

        for level, fill in enumerate(walk.fills, start=1):
            cum_qty += fill.size
            cum_amt += fill.amount
            fills.append(
                FillLevel(
                    level=level,
                    price=fill.price,
                    size=fill.size,
                    amount=fill.amount,
                    cumulative_quantity=cum_qty,
                    cumulative_amount=cum_amt,
                    cumulative_average=cum_amt / cum_qty if cum_qty > 0 else 0.0,
                )
            )
        return fills

    def _compute(
        self,
        book: OrderBook,
        side: OrderSide,
        *,
        amount: float | None,
        quantity: float | None,
        data_updated_at: int | None = None,
    ) -> SlippageResult:
        """호가창과 요청으로 결과를 만든다."""
        is_buy = side is OrderSide.BUY
        # 매수는 매도호가를, 매도는 매수호가를 훑는다.
        levels = book.asks if is_buy else book.bids
        best = book.best_ask if is_buy else book.best_bid

        if not levels or best is None or best <= 0:
            raise MarketDataNotFoundError(
                f"DB 에 저장된 {book.exchange} {book.native_symbol} 의 "
                f"{'매도' if is_buy else '매수'}호가가 비어 있습니다. "
                "POST /refresh 로 다시 수집하세요.",
                detail={"exchange": book.exchange, "native_symbol": book.native_symbol},
            )

        walk = (
            walk_by_amount(levels, amount)
            if amount is not None
            else walk_by_quantity(levels, quantity)
        )

        if walk.quantity <= 0:
            raise InvalidRequestError(
                "요청 규모가 너무 작아 최소 단위도 체결되지 않습니다.",
                detail={"amount": amount, "quantity": quantity},
            )

        slippage = walk.slippage_percent(best, is_buy=is_buy)

        # 슬리피지 손해액: 최우선 호가로 전부 체결됐다면 어땠을지와의 차이
        if is_buy:
            # 같은 돈으로 더 많이 살 수 있었다 → 못 산 수량을 평균가로 환산
            ideal_quantity = walk.amount / best
            slippage_cost = (ideal_quantity - walk.quantity) * walk.average_price
        else:
            # 같은 수량으로 더 많이 받을 수 있었다 → 덜 받은 금액
            slippage_cost = walk.quantity * best - walk.amount

        warnings: list[str] = []
        if walk.exhausted:
            warnings.append(
                f"호가 {walk.levels_consumed}단계를 모두 소진했습니다. "
                f"요청을 다 채우지 못했고 실제 체결분({walk.quantity:.8f} {book.base})만 "
                "계산에 반영되었습니다. depth 를 늘려도 수집 시 저장된 호가 단계를 "
                "넘지 못합니다."
            )
        if walk.levels_consumed == 1:
            warnings.append(
                "최우선 호가 1단계 안에서 끝나 슬리피지가 0 입니다. "
                "이보다 규모를 키우면 슬리피지가 생깁니다."
            )
        warnings.append(
            "DB 에 저장된 스냅샷 호가 기준의 값입니다. 주문 제출과 체결 사이의 "
            "가격 변동(타이밍 슬리피지)은 반영되지 않으며, 스냅샷이 오래됐으면 "
            "POST /refresh 로 갱신하세요."
        )

        top = levels[0]
        return SlippageResult(
            exchange=book.exchange,
            name=get_exchange(book.exchange).name,
            symbol=book.symbol,
            quote_currency=book.quote,
            side=side,
            requested_amount=amount,
            requested_quantity=quantity,
            best_price=best,
            average_price=walk.average_price,
            worst_price=walk.fills[-1].price if walk.fills else best,
            quantity=walk.quantity,
            amount=walk.amount,
            slippage_percent=slippage,
            slippage_cost=max(0.0, slippage_cost),
            levels_consumed=walk.levels_consumed,
            depth_exhausted=walk.exhausted,
            depth_available=len(levels),
            top_level_amount=top.price * top.size,
            fills=self._build_fills(walk),
            data_updated_at=data_updated_at,
            warnings=warnings,
        )

    async def calculate(
        self,
        session: AsyncSession,
        exchange_id: str,
        symbol: Symbol,
        *,
        side: OrderSide,
        amount: float | None = None,
        quantity: float | None = None,
        depth: int = DEFAULT_DEPTH,
    ) -> SlippageResult:
        """메모리 스냅샷(없으면 DB)으로 거래소 한 곳의 슬리피지를 계산한다.

        Args:
            session: DB 세션.
            exchange_id: 거래소 ID.
            symbol: 통일 심볼.
            side: 매수/매도.
            amount: 금액 기준으로 계산 (결제 통화). ``quantity`` 와 **택일**.
            quantity: 수량 기준으로 계산 (코인 개수). ``amount`` 와 **택일**.
            depth: 훑을 호가 단계 수. 저장된 단계 수를 넘으면 있는 만큼만 훑는다.

        Raises:
            InvalidRequestError: amount / quantity 를 둘 다 주거나 둘 다 안 준 경우.
            MarketDataNotFoundError: 스냅샷이 없거나, 저장된 마켓과 quote 가
                다른 심볼을 요청한 경우.
        """
        if (amount is None) == (quantity is None):
            raise InvalidRequestError(
                "amount 또는 quantity 중 정확히 하나만 지정해야 합니다. "
                "amount 는 금액 기준(결제 통화), quantity 는 코인 수량 기준입니다.",
                detail={"amount": amount, "quantity": quantity},
            )
        if (amount is not None and amount <= 0) or (
            quantity is not None and quantity <= 0
        ):
            raise InvalidRequestError(
                "amount / quantity 는 0 보다 커야 합니다.",
                detail={"amount": amount, "quantity": quantity},
            )

        # 거래소 ID 검증 + 표시용 이름. 메타데이터만 쓰고 API 호출은 하지 않는다.
        exchange = get_exchange(exchange_id)

        snap = await require_snapshot_or_db(session, exchange.id, symbol.base)
        if symbol.quote != snap.quote:
            raise MarketDataNotFoundError(
                f"{exchange.id} 거래소에 {symbol} 마켓이 없습니다. "
                f"{snap.base} 는 {snap.base}/{snap.quote} 마켓으로 저장되어 있습니다 — "
                f"quote 를 {snap.quote} 로 바꿔 요청하세요.",
                detail={
                    "exchange": exchange.id,
                    "requested": str(symbol),
                    "stored": f"{snap.base}/{snap.quote}",
                },
            )

        book = repository.orderbook_from_snapshot(snap, depth=depth)
        return self._compute(
            book,
            side,
            amount=amount,
            quantity=quantity,
            data_updated_at=_epoch_ms(snap.updated_at),
        )


slippage_service = SlippageService()
