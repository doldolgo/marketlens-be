"""김치 프리미엄 / 역프리미엄 라우터 — DB 스냅샷 기반.

방향별로 엔드포인트를 나눴다. 두 방향은 부호만 뒤집은 값이 아니라
**서로 다른 거래**이기 때문이다.

거래소를 직접 호출하지 않는다. ``POST /refresh`` 가 저장해둔
``market_snapshots`` / ``usdkrw_rate`` 를 읽어서만 계산한다.
"""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_session
from app.models.premium import PremiumDirection, PremiumResult
from app.models.scan import ScanResult, SortOrder
from app.services.premium_service import premium_service
from app.services.scan_service import scan_service

router = APIRouter(prefix="/premium", tags=["premium"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]

SymQuery = Annotated[str, Query(description="조회할 코인 심볼", examples=["BTC"])]
FxQuery = Annotated[
    list[str] | None,
    Query(
        description=(
            "비교할 **해외** 거래소 ID. 반복 지정 가능 (`&fx=binance`). "
            "생략하면 DB 에 USDT 스냅샷이 있는 모든 거래소 (국내 기준 거래소 제외)"
        )
    ),
]
DomQuery = Annotated[
    str | None,
    Query(
        description=(
            "**국내 거래소** ID (업비트 / 빗썸 등). 김프의 원화 축이 된다. "
            "생략하면 설정 기본값. 환율은 어느 거래소든 통일 환율"
            "(하나은행 고시 USD/KRW) 하나다"
        ),
        examples=["upbit"],
    ),
]
_SHARED_DESC = """
### 가격 기준

**실제로 체결되는 쪽 호가**를 쓴다 — 살 때는 매도호가(ask), 팔 때는
매수호가(bid). 방향에 따라 쓰는 호가가 달라지므로 김프/역김프 값은
서로 독립적이다.

### 데이터 출처

거래소를 직접 호출하지 않고 **DB 스냅샷만 읽는다.** 데이터가 오래됐으면
`data_oldest_at` 으로 알 수 있고, `POST /refresh` 로 갱신한다.

환율은 `usdkrw_rate` 에 저장된 **하나은행 고시 USD/KRW 매매기준율** 하나다 —
어느 국내 거래소를 기준으로 하든 같은 환율이 적용된다.

> ⚠️ 거래 수수료·출금 수수료·전송 시간은 반영하지 않은 이론값이다.
> 실제 금액을 넣었을 때의 수익은 `/arbitrage`, 전 조합은 `/matrix` 로 확인할 것.

테스트 예시 (배포 서버):
    http://3.34.104.16:8000/premium/fwd?sym=BTC        (김프)
    http://3.34.104.16:8000/premium/rev?sym=BTC        (역프)
    http://3.34.104.16:8000/premium?sym=BTC            (양방향)
    http://3.34.104.16:8000/premium/scan?limit=5       (전종목 스캔)
"""


@router.get(
    "/fwd",
    response_model=PremiumResult,
    summary="김프 — 해외에서 사와서 국내에 팔 때",
    description=(
        "**해외 매수 → 국내 매도** 방향의 수익률을 거래소별로 계산한다. "
        "흔히 말하는 김치 프리미엄이 이 방향이다.\n\n"
        "```\n"
        "수익률 = 국내 매도가 / 해외 매수가(원화 환산) - 1\n"
        "```\n\n"
        "**양수면 이 방향이 이득**(국내가 비쌈), 음수면 손해다.\n\n"
        "**해외는 매도호가(ask), 국내는 매수호가(bid)** 를 쓴다.\n"
        + _SHARED_DESC
    ),
)
async def get_fwd_premium(
    session: SessionDep,
    sym: SymQuery,
    dom: DomQuery = None,
    fx: FxQuery = None,
) -> PremiumResult:
    return await premium_service.fetch_premiums(
        session,
        sym,
        direction=PremiumDirection.FWD,
        domestic=dom,
        exchanges=fx,
    )


@router.get(
    "/rev",
    response_model=PremiumResult,
    summary="역김프 — 국내에서 사서 해외에 팔 때",
    description=(
        "**국내 매수 → 해외 매도** 방향의 수익률을 거래소별로 계산한다.\n\n"
        "```\n"
        "수익률 = 해외 매도가(원화 환산) / 국내 매수가 - 1\n"
        "```\n\n"
        "**양수면 이 방향이 이득**(해외가 비쌈), 음수면 손해다.\n\n"
        "**국내는 매도호가(ask), 해외는 매수호가(bid)** 를 쓴다.\n\n"
        "> 김프의 단순한 부호 반전이 아니다. 두 방향은 쓰는 호가가 아예 달라서 "
        "**동시에 음수일 수 있다** — 스프레드가 가격차를 다 먹은 상태이며 정상이다.\n"
        + _SHARED_DESC
    ),
)
async def get_rev_premium(
    session: SessionDep,
    sym: SymQuery,
    dom: DomQuery = None,
    fx: FxQuery = None,
) -> PremiumResult:
    return await premium_service.fetch_premiums(
        session,
        sym,
        direction=PremiumDirection.REV,
        domestic=dom,
        exchanges=fx,
    )


@router.get(
    "/scan",
    response_model=ScanResult,
    summary="전종목 스캔 — 김프 1등 · 역김프 1등",
    description=(
        "국내에 상장된 **모든 코인**을 훑어 두 방향 각각의 수익률 1등을 찾는다.\n\n"
        "- `best_fwd` — 김프(해외 매수 → 국내 매도) 수익률이 가장 높은 코인\n"
        "- `best_rev` — 역김프(국내 매수 → 해외 매도) 수익률이 가장 높은 코인\n"
        "- `top_fwd` / `top_rev` — 각 방향 상위 목록\n\n"
        "수익률 계산식은 `/premium/fwd` · `/premium/rev` 와 **완전히 동일**하다.\n\n"
        "### 데이터 출처\n\n"
        "거래소를 직접 호출하지 않고 **DB 스냅샷만 읽는다** — 국내(KRW) 전종목 × "
        "해외(USDT) 스냅샷의 교집합을 돈다. 데이터가 오래됐으면 `data_oldest_at` "
        "으로 알 수 있고, `POST /refresh` 로 갱신한다.\n\n"
        "### 가격 기준\n\n"
        "**실제로 체결되는 쪽 호가**를 쓴다 — 살 때 매도호가(ask), 팔 때 "
        "매수호가(bid). 오래된 체결가가 1등으로 올라오는 유령 값이 걸러진다. "
        "`min_liquidity_krw` 로 얇은 호가까지 함께 걸러내는 것을 권한다.\n\n"
        "금액 기반 전종목 계산은 `GET /matrix` 를 사용할 것."
    ),
)
async def scan_premiums(
    session: SessionDep,
    dom: DomQuery = None,
    fx: FxQuery = None,
    min_liquidity_krw: Annotated[
        float,
        Query(
            ge=0,
            description=(
                "저장된 최우선 호가의 체결 가능 금액(잔량 × 가격)이 이보다 작은 "
                "조합은 제외한다 (원화). `0` 이면 필터 없음"
            ),
            examples=[1000000],
        ),
    ] = 0.0,
    limit: Annotated[int, Query(ge=1, le=100, description="방향별 목록 개수")] = 10,
    order: Annotated[
        SortOrder,
        Query(
            description=(
                "`top_fwd` / `top_rev` 정렬 방향. "
                "`asc`=수익률 오름차순(기본), `desc`=내림차순. "
                "`best_fwd` / `best_rev` 는 정렬과 무관하게 **항상 최대값**"
            )
        ),
    ] = SortOrder.ASC,
) -> ScanResult:
    return await scan_service.scan(
        session,
        domestic=dom,
        exchanges=fx,
        min_liquidity_krw=min_liquidity_krw,
        limit=limit,
        order=order,
    )


class PremiumSearchResult(BaseModel):
    """코인 하나에 대한 김프 · 역김프 동시 조회 결과."""

    sym: str = Field(..., description="조회한 코인 심볼")

    fwd: PremiumResult = Field(..., description="김프 — 해외 매수 → 국내 매도")
    rev: PremiumResult = Field(..., description="역김프 — 국내 매수 → 해외 매도")

    best_direction: PremiumDirection | None = Field(
        None,
        description=(
            "두 방향 중 수익률이 높은 쪽. **둘 다 손해면 그중 덜 나쁜 쪽**이므로 "
            "`profitable` 을 함께 확인해야 한다. 계산 불가면 null"
        ),
    )
    best_premium_percent: float | None = Field(
        None, description="`best_direction` 방향의 수익률 (%)"
    )

    data_oldest_at: int | None = Field(
        None,
        description=(
            "두 방향에서 사용한 스냅샷 중 **가장 오래된** 갱신 시각 (epoch ms). "
            "지금과의 차이가 크면 POST /refresh 로 갱신할 것"
        ),
    )
    data_newest_at: int | None = Field(
        None, description="사용한 스냅샷 중 가장 최근 갱신 시각 (epoch ms)"
    )
    data_received_at: int | None = Field(
        None,
        description=(
            "이 응답의 데이터를 **거래소에서 받은** 시각 (epoch ms). "
            "두 방향이 같은 사이클의 데이터이므로 하나의 값이다"
        ),
    )

    fetched_at: int = Field(..., description="서버 응답 생성 시각 (epoch ms)")
    elapsed_ms: float = Field(..., description="전체 처리 시간 (ms)")


@router.get(
    "",
    response_model=PremiumSearchResult,
    summary="코인 검색 — 김프와 역김프를 한 번에",
    description=(
        "코인 하나를 검색하면 **김프와 역김프를 동시에** 반환한다.\n\n"
        "`/premium/fwd` 와 `/premium/rev` 를 각각 부르는 것과 결과가 같지만, "
        "왕복이 한 번으로 줄어든다. 데이터는 같은 DB 스냅샷이므로 두 방향이 "
        "**같은 시점 기준**으로 계산된다.\n\n"
        "`best_direction` 은 두 방향 중 수익률이 높은 쪽이다. "
        "다만 **둘 다 손해일 때는 '덜 나쁜 쪽'** 이므로 해당 방향의 `profitable` 을 "
        "반드시 함께 확인해야 한다.\n\n"
        "`dom` 으로 국내 거래소를 고르면 그 거래소 기준으로 계산된다. "
        "환율은 어느 거래소든 통일 환율(`usdkrw_rate`) 하나다."
    ),
)
async def search_premium(
    session: SessionDep,
    sym: SymQuery,
    dom: DomQuery = None,
    fx: FxQuery = None,
) -> PremiumSearchResult:
    started = time.perf_counter()

    # 둘 다 같은 DB 세션에서 읽으므로 거래소 호출 시절과 달리 병렬화가 필요 없다.
    fwd = await premium_service.fetch_premiums(
        session,
        sym,
        direction=PremiumDirection.FWD,
        domestic=dom,
        exchanges=fx,
    )
    rev = await premium_service.fetch_premiums(
        session,
        sym,
        direction=PremiumDirection.REV,
        domestic=dom,
        exchanges=fx,
    )

    # 각 방향의 최고 수익률끼리 비교한다.
    best_of = {
        PremiumDirection.FWD: max(
            (e.premium_percent for e in fwd.premiums), default=None
        ),
        PremiumDirection.REV: max(
            (e.premium_percent for e in rev.premiums), default=None
        ),
    }
    available = {d: v for d, v in best_of.items() if v is not None}
    best_direction = max(available, key=available.get) if available else None

    oldests = [
        v for v in (fwd.data_oldest_at, rev.data_oldest_at) if v is not None
    ]
    newests = [
        v for v in (fwd.data_newest_at, rev.data_newest_at) if v is not None
    ]

    return PremiumSearchResult(
        sym=fwd.sym,
        fwd=fwd,
        rev=rev,
        best_direction=best_direction,
        best_premium_percent=available.get(best_direction) if best_direction else None,
        data_oldest_at=min(oldests) if oldests else None,
        data_newest_at=max(newests) if newests else None,
        # 두 방향이 같은 사이클을 읽으므로 값이 같다 — 혹시 갈리면 오래된 쪽.
        data_received_at=min(
            (v for v in (fwd.data_received_at, rev.data_received_at) if v is not None),
            default=None,
        ),
        fetched_at=int(time.time() * 1000),
        elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
    )
