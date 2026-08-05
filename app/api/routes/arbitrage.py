"""금액 기준 차익거래 시뮬레이션 라우터."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.models.arbitrage import ArbitrageResult
from app.models.orderbook import MarketType
from app.services.arbitrage_service import DEFAULT_DEPTH, arbitrage_service

router = APIRouter(prefix="/arbitrage", tags=["arbitrage"])


@router.get(
    "",
    response_model=ArbitrageResult,
    summary="투입 금액 기준 차익 계산",
    description=(
        "**금액을 넣으면 실제로 얼마가 남는지** 계산한다.\n\n"
        "1. 대상 거래소 호가를 모두 원화로 환산한다\n"
        "2. 최우선 매도호가가 **가장 싼 곳**에서 매수, "
        "최우선 매수호가가 **가장 비싼 곳**에서 매도하도록 방향을 잡는다\n"
        "3. 투입 금액만큼 매수처의 매도호가를 **시장가로 훑어** 코인 수량을 구한다\n"
        "4. 그 수량을 매도처의 매수호가에 **시장가로 훑어** 수령액을 구한다\n"
        "5. 두 금액의 차이가 차익이다\n\n"
        "`/premium` 은 최우선 호가 한 점만 보지만 여기서는 호가창을 실제로 소진시킨다. "
        "그래서 금액이 커질수록 결과가 프리미엄보다 나빠진다(슬리피지). "
        "`premium_capture_percent` 가 그 손실 정도를 보여준다.\n\n"
        "프리미엄이 음수(역프)면 매수/매도 방향이 자동으로 뒤집힌다.\n\n"
        "⚠️ 거래 수수료·출금 수수료·전송 시간은 반영하지 않은 이론값이다."
    ),
)
async def simulate_arbitrage(
    base: Annotated[
        str, Query(description="대상 코인 심볼", examples=["BTC"])
    ],
    amount: Annotated[
        float, Query(gt=0, description="투입 금액", examples=[10000000])
    ],
    currency: Annotated[
        str, Query(description="투입 금액의 통화. `KRW` 또는 `USDT`")
    ] = "KRW",
    exchanges: Annotated[
        list[str] | None,
        Query(
            description=(
                "대상 거래소 ID. 반복 지정 가능 (`&exchanges=upbit&exchanges=binance`). "
                "생략하면 해당 마켓을 지원하는 모든 거래소"
            )
        ),
    ] = None,
    market_type: Annotated[
        MarketType, Query(description="현물(spot) / 선물(futures)")
    ] = MarketType.SPOT,
    depth: Annotated[
        int,
        Query(
            ge=1,
            le=1000,
            description="훑을 호가 단계 수. 업비트는 최대 30단계까지만 제공한다",
        ),
    ] = DEFAULT_DEPTH,
) -> ArbitrageResult:
    return await arbitrage_service.simulate(
        base,
        amount=amount,
        currency=currency,
        exchanges=exchanges,
        market_type=market_type,
        depth=depth,
    )
