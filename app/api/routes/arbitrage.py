"""금액 기준 차익거래 시뮬레이션 라우터 — DB 스냅샷 기반."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_session
from app.models.arbitrage import ArbitrageResult
from app.models.premium import PremiumDirection
from app.services.arbitrage_service import DEFAULT_DEPTH, arbitrage_service

router = APIRouter(prefix="/arbitrage", tags=["arbitrage"])


@router.get(
    "",
    response_model=ArbitrageResult,
    summary="투입 금액 기준 차익 계산",
    description=(
        "**금액을 넣으면 실제로 얼마가 남는지** 계산한다.\n\n"
        "거래소를 직접 호출하지 않고 **DB 스냅샷만 읽는다.** 데이터가 오래됐으면 "
        "`data_oldest_at` 으로 알 수 있고, `POST /refresh` 로 갱신한다.\n\n"
        "1. 대상 코인의 저장된 호가를 모두 `currency` 통화로 환산한다 "
        "(환율 = 국내 거래소 KRW-USDT 호가. **환전도 체결되는 쪽 호가**를 쓰므로 "
        "매수측과 매도측에 각각 ask/bid 가 적용된다)\n"
        "2. 최우선 매도호가가 **가장 싼 곳**에서 매수, "
        "최우선 매수호가가 **가장 비싼 곳**에서 매도하도록 방향을 잡는다\n"
        "3. 투입 금액만큼 매수처의 매도호가를 **시장가로 훑어** 코인 수량을 구한다\n"
        "4. 그 수량을 매도처의 매수호가에 **시장가로 훑어** 수령액을 구한다\n"
        "5. 두 금액의 차이가 차익이다\n\n"
        "`/premium` 은 최우선 호가 한 점만 보지만 여기서는 호가창을 실제로 소진시킨다. "
        "그래서 금액이 커질수록 결과가 프리미엄보다 나빠진다(슬리피지). "
        "`premium_capture_percent` 가 그 손실 정도를 보여준다.\n\n"
        "`direction` 을 지정하면 그 방향으로 **고정**되어 손해(음수)도 그대로 보여준다. "
        "생략하면 이득이 나는 방향을 자동으로 고른다.\n\n"
        "⚠️ 거래 수수료·출금 수수료·전송 시간은 반영하지 않은 이론값이다."
    ),
)
async def simulate_arbitrage(
    session: Annotated[AsyncSession, Depends(get_session)],
    sym: Annotated[str, Query(description="대상 코인 심볼", examples=["BTC"])],
    amount: Annotated[float, Query(gt=0, description="투입 금액", examples=[10000000])],
    currency: Annotated[
        str,
        Query(description="투입 금액의 통화이자 호가 환산 기준. `KRW` 또는 `USDT`"),
    ] = "KRW",
    exchanges: Annotated[
        list[str] | None,
        Query(
            description=(
                "대상 거래소 ID. 반복 지정 가능 (`&exchanges=upbit&exchanges=binance`). "
                "생략하면 DB 에 스냅샷이 있는 모든 거래소"
            )
        ),
    ] = None,
    direction: Annotated[
        PremiumDirection | None,
        Query(
            description=(
                "차익 방향 고정. `fwd`=해외(USDT 마켓) 매수→국내(KRW 마켓) 매도, "
                "`rev`=국내 매수→해외 매도. "
                "**생략하면 가장 싼 곳↔가장 비싼 곳을 자동 선택**한다 — 가능한 "
                "조합 중 가장 유리한 것일 뿐, 스프레드가 가격차보다 크면 음수 "
                "수익이 나올 수 있다 (`warnings` 로 표시). "
                "방향을 지정하면 손해(음수)가 나올 수 있고 그게 정상이다. "
                "지정 시 `exchanges` 는 **해외** 거래소 목록으로 해석되고 국내는 자동 포함"
            )
        ),
    ] = None,
    depth: Annotated[
        int,
        Query(
            ge=1,
            le=1000,
            description=(
                "훑을 호가 단계 수. DB 에는 서버 설정 `ORDERBOOK_MAX_AMOUNT_KRW` "
                "금액을 커버하는 단계까지만 저장돼 있다"
            ),
        ),
    ] = DEFAULT_DEPTH,
) -> ArbitrageResult:
    return await arbitrage_service.simulate(
        session,
        sym,
        amount=amount,
        currency=currency,
        exchanges=exchanges,
        direction=direction,
        depth=depth,
    )
