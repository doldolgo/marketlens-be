"""매트릭스 라우터 — DB 스냅샷 기반 전 코인 김프·역프.

테스트 예시 (1천만원 투입 기준):
    http://3.34.104.16:8000/matrix?amount_krw=10000000
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_session
from app.models.matrix import MatrixResult
from app.services.matrix_service import matrix_service

router = APIRouter(prefix="/matrix", tags=["matrix"])


@router.get(
    "",
    response_model=MatrixResult,
    summary="전 코인 매트릭스 — 코인별 최대 김프·최대 역프",
    description=(
        "**국내(업비트·빗썸)와 해외(바이낸스)에 모두 상장된 모든 코인**에 대해 "
        "한 행씩 반환한다.\n\n"
        "각 행에는:\n\n"
        "- 국내 현재가 (KRW)\n"
        "- **가장 큰 김프** 조합 — 구매처 · 판매처 · 표면 프리미엄 · 실현 수익률 · "
        "슬리피지 · 구매처 출금 가능 여부 · 판매처 입금 가능 여부\n"
        "- **가장 큰 역프** 조합 — 위와 동일 (구매처·판매처는 김프와 다를 수 있다)\n\n"
        "거래소를 직접 호출하지 않고 **DB 스냅샷만 읽는다.** 데이터가 오래됐으면 "
        "`data_oldest_at` 으로 알 수 있고, `POST /refresh` 로 갱신한다.\n\n"
        "슬리피지는 `amount_krw` 한 개 금액에 대해 저장된 호가를 실제로 훑어 계산한다. "
        "저장 호가는 서버 설정 `ORDERBOOK_MAX_AMOUNT_KRW` 금액까지 커버하므로, "
        "그보다 큰 금액을 넣으면 `depth_exhausted` 가 표시된다."
    ),
)
async def get_matrix(
    session: Annotated[AsyncSession, Depends(get_session)],
    amount_krw: Annotated[
        float,
        Query(
            gt=0,
            description="슬리피지 계산에 쓸 투입 금액 (원화)",
            examples=[10_000_000],
        ),
    ] = 10_000_000.0,
) -> MatrixResult:
    return await matrix_service.build(session, amount_krw=amount_krw)
