"""슬리피지 라우터 — DB 스냅샷 호가 기반.

테스트 예시 (업비트 BTC 1천만원 매수 시 슬리피지):
    http://3.34.104.16:8000/slippage/upbit?symbol=BTC/KRW&amount=10000000
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_session
from app.models.slippage import OrderSide, SlippageResult
from app.models.symbol import Symbol
from app.services.slippage_service import DEFAULT_DEPTH, slippage_service

router = APIRouter(prefix="/slippage", tags=["slippage"])


@router.get(
    "/{exchange_id}",
    response_model=SlippageResult,
    summary="슬리피지 계산 — 최우선 호가 대비 얼마나 불리해지나",
    description=(
        "거래소 한 곳에서 **시장가로 거래하면 평균 체결가가 얼마나 나빠지는지** 계산한다.\n\n"
        "```\n"
        "슬리피지(%) = (평균 체결가 - 최우선 호가) / 최우선 호가 × 100\n"
        "```\n\n"
        "매수·매도 모두 **항상 0 이상**이다 (나에게 불리해진 정도).\n\n"
        "거래소를 직접 호출하지 않고 **DB 에 저장된 호가 스냅샷만 읽는다.** "
        "데이터가 얼마나 신선한지는 응답의 `data_updated_at` 으로 알 수 있고, "
        "오래됐으면 `POST /refresh` 로 갱신한다.\n\n"
        "### 왜 생기나\n\n"
        "한 호가 단계에는 정해진 잔량만 있다. 그보다 많이 거래하면 다음 단계로 파고들며 "
        "가격이 불리해진다. **최우선 호가 1단계 안에서 끝나면 슬리피지는 0** 이고, "
        "규모가 커질수록 커진다.\n\n"
        "### 금액 기준 / 수량 기준\n\n"
        "`amount` 와 `quantity` 중 **정확히 하나**를 지정한다.\n\n"
        '- `amount` — "1억원어치 사면?" (그 마켓의 결제 통화 기준 그대로, 환산 없음. '
        "KRW 마켓이면 원, USDT 마켓이면 USDT)\n"
        '- `quantity` — "0.5 BTC 팔면?" (코인 수량 기준)\n\n'
        "### 응답의 `fills`\n\n"
        "단계별 체결 내역이다. 업비트 호가창에서 마우스를 올리면 뜨는 툴팁"
        "(평균가 · 누적량 · 누적액)과 **같은 값**이다.\n\n"
        "> ⚠️ 저장된 스냅샷 호가 기준이다. 주문 제출과 체결 사이의 가격 변동"
        "(타이밍 슬리피지)은 계산할 수 없으므로 반영되지 않는다. "
        "실전 슬리피지는 이 값보다 크다."
    ),
)
async def get_slippage(
    exchange_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    symbol: Annotated[
        str,
        Query(description="통일 심볼. BASE/QUOTE 형식", examples=["BTC/KRW"]),
    ],
    side: Annotated[
        OrderSide,
        Query(description="`buy`=매수(매도호가를 훑음) / `sell`=매도(매수호가를 훑음)"),
    ] = OrderSide.BUY,
    amount: Annotated[
        float | None,
        Query(
            gt=0,
            description=(
                "**금액** 기준 (결제 통화). 예: KRW 마켓이면 원, USDT 마켓이면 USDT. "
                "`quantity` 와 택일"
            ),
            examples=[100000000],
        ),
    ] = None,
    quantity: Annotated[
        float | None,
        Query(
            gt=0,
            description="**코인 수량** 기준. `amount` 와 택일",
            examples=[0.5],
        ),
    ] = None,
    depth: Annotated[
        int,
        Query(
            ge=1,
            le=1000,
            description=(
                "훑을 호가 단계 수. DB 에 저장된 단계 수를 넘으면 있는 만큼만 훑는다"
            ),
        ),
    ] = DEFAULT_DEPTH,
) -> SlippageResult:
    return await slippage_service.calculate(
        session,
        exchange_id,
        Symbol.parse(symbol),
        side=side,
        amount=amount,
        quantity=quantity,
        depth=depth,
    )
