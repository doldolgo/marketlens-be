"""호가 조회 라우터 — DB 스냅샷 기반.

거래소를 직접 호출하지 않는다. ``POST /refresh`` 가 저장해둔
``market_snapshots`` 의 호가를 기존 :class:`OrderBook` 모델로 되살려 반환한다.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import MarketDataNotFoundError
from app.db import repository
from app.db.database import get_session
from app.exchanges.registry import get_exchange
from app.models.orderbook import OrderBookLevel
from app.models.symbol import Symbol

router = APIRouter(prefix="/orderbook", tags=["orderbook"])


class OrderBookResponse(BaseModel):
    """호가 조회 응답 — 내부 수집 모델(OrderBook)에서 API 에 필요한 것만 담는다."""

    exchange: str = Field(..., description="거래소 ID (예: upbit, binance)")
    symbol: str = Field(..., description="통일 심볼 (예: BTC/KRW, BTC/USDT)")
    base: str = Field(..., description="기준 통화 (예: BTC)")
    quote: str = Field(..., description="결제 통화 (예: KRW, USDT)")

    bids: list[OrderBookLevel] = Field(
        default_factory=list, description="매수 호가, 가격 내림차순"
    )
    asks: list[OrderBookLevel] = Field(
        default_factory=list, description="매도 호가, 가격 오름차순"
    )

    timestamp: int = Field(
        ..., description="거래소 기준 호가 시각 (epoch milliseconds)"
    )
    data_updated_at: int | None = Field(
        None,
        description=(
            "DB 스냅샷 갱신 시각 (epoch ms). "
            "지금과의 차이가 크면 POST /refresh 로 갱신할 것"
        ),
    )

SymbolQuery = Annotated[
    str,
    Query(
        description="통일 심볼. BASE/QUOTE 형식 (예: BTC/KRW, BTC/USDT)",
        examples=["BTC/KRW"],
    ),
]
DepthQuery = Annotated[
    int,
    Query(
        ge=1,
        le=100,
        description="반환할 호가 단계 수. DB 에 저장된 깊이 안에서 자르기만 한다.",
    ),
]


@router.get(
    "/{exchange_id}",
    response_model=OrderBookResponse,
    summary="단일 거래소 호가 조회",
    description=(
        "지정한 거래소의 호가창을 **DB 스냅샷에서** 조회한다.\n\n"
        "거래소를 직접 호출하지 않고 `POST /refresh` 가 저장해둔 호가를 반환한다. "
        "데이터가 없으면 404 — 먼저 수집했는지, 그 거래소에 상장된 코인인지 확인한다.\n\n"
        "- `symbol` 의 QUOTE 는 저장된 마켓과 일치해야 한다 "
        "(예: 업비트는 KRW 마켓, 바이낸스는 USDT 마켓).\n"
        "- `depth` 는 저장된 깊이 안에서 **자르기만 한다** — 저장 단계보다 큰 값을 줘도 "
        "저장된 만큼만 반환된다.\n"
        "- `timestamp` 는 수집 시점에 거래소가 준 시세 시각(epoch ms)이다. "
        "오래됐으면 `POST /refresh` 로 갱신한다."
    ),
)
async def get_orderbook(
    exchange_id: str,
    symbol: SymbolQuery,
    session: Annotated[AsyncSession, Depends(get_session)],
    depth: DepthQuery = settings.default_orderbook_depth,
) -> OrderBookResponse:
    # 거래소 ID 검증(등록 여부)에만 registry 를 쓴다. API 호출은 하지 않는다.
    exchange = get_exchange(exchange_id)
    parsed = Symbol.parse(symbol)

    snap = await repository.require_snapshot(session, exchange.id, parsed.base)

    # DB 는 거래소당 한 마켓(현물)만 저장하므로, 요청한 QUOTE 가 다르면 404 로 안내한다.
    if parsed.quote != snap.quote:
        raise MarketDataNotFoundError(
            f"{exchange.id} 거래소에는 {parsed.base}가 {snap.quote} 마켓으로 "
            f"저장되어 있습니다. '{parsed.base}/{snap.quote}' 로 다시 요청하세요.",
            detail={
                "exchange": exchange.id,
                "base": parsed.base,
                "requested_quote": parsed.quote,
                "stored_quote": snap.quote,
            },
        )

    book = repository.orderbook_from_snapshot(snap, depth=depth)
    return OrderBookResponse(
        exchange=book.exchange,
        symbol=book.symbol,
        base=book.base,
        quote=book.quote,
        bids=book.bids,
        asks=book.asks,
        timestamp=book.timestamp,
        data_updated_at=book.data_updated_at,
    )
