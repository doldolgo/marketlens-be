"""스프레드 테이블 라우터 — FE 스프레드 탭의 단일 데이터 소스.

거래소를 직접 호출하지 않는다. ``POST /refresh`` 가 저장해둔
DB 스냅샷을 읽어서만 계산한다.

테스트 예시:
    http://3.34.104.16:8000/spreads
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.database import get_session
from app.models.spread import SpreadsResult
from app.services.spread_service import spread_service

router = APIRouter(prefix="/spreads", tags=["spreads"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.get(
    "",
    response_model=SpreadsResult,
    response_model_by_alias=True,
    summary="스프레드 테이블 — 전 페어 김프/역프 한 번에",
    description=(
        "**(국내 거래소 × 해외 거래소 × 코인)** 페어마다 김프(`fwd`)와 "
        "역프(`rev`)를 한 행에 담아 전부 반환한다. FE 스프레드 탭이 이 응답을 "
        "그대로 그린다.\n\n"
        "수익률 계산식은 `/premium/fwd` · `/premium/rev` 와 **완전히 동일**하다 — "
        "체결되는 쪽 호가(살 때 ask, 팔 때 bid)를 쓴다.\n\n"
        "### 행별 필드\n\n"
        "- `usd` — 해외 USD(T) 마지막 체결가. 원화 환산은 최상위 `rate` 를 곱한다\n"
        "- `liqDom` / `liqFx` — 최우선 호가 유동성 (USD 환산, 양쪽 중 작은 쪽). "
        "슬리피지 추정용\n"
        f"- `status` — `ok` / `stale`(갱신 후 {settings.spread_stale_seconds:.0f}초 "
        "초과) / `fail`(저장 호가가 비어 계산 불가 — 값은 0)\n"
        "- `age` — 스냅샷 마지막 갱신 후 경과 초 (양측 중 오래된 쪽)\n"
        "- `spark` — 프리미엄 추이. 이력 저장 전까지는 **항상 빈 배열**\n\n"
        "필터 없이 전체를 반환한다 — 거래소·코인 필터링은 FE 가 담당한다.\n"
        "데이터가 오래됐으면 `POST /refresh` 로 갱신한다."
    ),
)
async def get_spreads(session: SessionDep) -> SpreadsResult:
    return await spread_service.build(session)
