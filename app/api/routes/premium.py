"""김치 프리미엄 라우터."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.models.orderbook import MarketType
from app.models.premium import PremiumResult
from app.models.ticker import PriceBasis
from app.services.premium_service import premium_service

router = APIRouter(prefix="/premium", tags=["premium"])


@router.get(
    "",
    response_model=PremiumResult,
    summary="거래소별 김치 프리미엄 조회",
    description=(
        "원화 가격이 해외 가격보다 몇 % 비싼지를 거래소별로 계산한다.\n\n"
        "```\n"
        "프리미엄 비율 = 원화 가격 / 해외 가격 / 환율\n"
        "프리미엄 (%) = (비율 - 1) × 100\n"
        "```\n\n"
        "- **원화 가격**: 업비트 KRW 마켓 (고정)\n"
        "- **해외 가격**: 요청한 거래소들의 USDT 마켓. 생략하면 지원되는 전체\n"
        "- **환율**: 업비트 `KRW-USDT`\n\n"
        "양수면 국내가 비싼 것(김프), 음수면 국내가 싼 것(역프)이다.\n\n"
        "세 가격은 모두 `price_basis` 가 지정한 **같은 기준**으로 뽑는다. "
        "기본값은 마지막 체결가(`last`)이며, 이것이 통상적인 김프 계산 방식이다."
    ),
)
async def get_premium(
    base: Annotated[
        str, Query(description="조회할 코인 심볼", examples=["BTC"])
    ],
    exchanges: Annotated[
        list[str] | None,
        Query(
            description=(
                "비교할 거래소 ID. 반복 지정 가능 (`&exchanges=binance`). "
                "생략하면 USDT 마켓을 지원하는 모든 거래소 (업비트 제외)"
            )
        ),
    ] = None,
    market_type: Annotated[
        MarketType, Query(description="해외 거래소 쪽 시장 구분")
    ] = MarketType.SPOT,
    price_basis: Annotated[
        PriceBasis,
        Query(
            description=(
                "가격 기준. `last`=마지막 체결가(기본), `mid`=최우선 호가 중간값. "
                "체결이 뜸한 종목은 `last` 가 오래된 값일 수 있어 `mid` 가 유용하다"
            )
        ),
    ] = PriceBasis.LAST,
) -> PremiumResult:
    return await premium_service.fetch_premiums(
        base,
        exchanges=exchanges,
        market_type=market_type,
        price_basis=price_basis,
    )
