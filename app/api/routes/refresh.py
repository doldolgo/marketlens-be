"""DB 갱신 라우터.

이 백엔드에서 거래소 API 를 실제로 호출하는 엔드포인트는 이것 하나다.
나머지 모든 조회 API 는 여기서 저장한 DB 를 읽는다.
김프/역프 기록(premium_archive)과 플랫폼 상태(platform_status)도 이때 함께 갱신된다.

테스트 예시:
    curl -X POST -H "X-Refresh-Token: <토큰>" http://3.34.104.16:8000/refresh
"""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.database import get_session
from app.models.refresh import RefreshResult
from app.services.collector_service import collector_service

router = APIRouter(prefix="/refresh", tags=["refresh"])


def _check_refresh_token(
    x_refresh_token: Annotated[str | None, Header()] = None,
) -> None:
    """REFRESH_TOKEN 이 설정돼 있으면 헤더 토큰을 요구한다.

    refresh 는 거래소 호출이 수백 회 나가는 비싼 작업이라, 공개 배포 시
    아무나 트리거하면 rate limit 이 소진된다. 토큰이 비어 있으면(로컬 개발)
    검사하지 않는다.
    """
    if not settings.refresh_token:
        return
    if x_refresh_token is None or not hmac.compare_digest(
        x_refresh_token, settings.refresh_token
    ):
        raise HTTPException(
            status_code=401,
            detail="X-Refresh-Token 헤더가 없거나 올바르지 않습니다.",
        )


@router.post(
    "",
    response_model=RefreshResult,
    summary="DB 갱신 — 거래소에서 시세·호가·입출금 상태를 수집해 저장",
    description=(
        "거래소 API 를 호출해 DB 를 통째로 갱신한다.\n\n"
        "수집 대상\n\n"
        "| 데이터 | 출처 | 저장 위치 |\n"
        "|---|---|---|\n"
        "| KRW 전종목 현재가 + 호가 | 업비트 · 빗썸 (일괄 조회) | `market_snapshots` |\n"
        "| USDT 마켓 현재가 + 호가 | 바이낸스 (국내 상장 코인만, 심볼별) | `market_snapshots` |\n"
        "| 입출금 가능 여부 | 업비트 · 바이낸스 (API 키 필요) · 빗썸 (public) | `market_snapshots` |\n"
        "| USD/KRW 환율 | 하나은행 고시 (매매기준율) | `usdkrw_rate` |\n\n"
        "가격과 호가는 **환산 없이 그 거래소 통화 그대로** 저장된다 "
        "(업비트·빗썸 = KRW, 바이낸스 = USDT). 원화 환산은 조회 시점에 "
        "`usdkrw_rate` (하나은행 고시 USD/KRW) 를 곱해서 한다.\n\n"
        "호가는 설정된 최대 금액(`ORDERBOOK_MAX_AMOUNT_KRW`)의 체결을 커버하는 "
        "깊이까지만 저장된다.\n\n"
        "API 키가 없으면 입출금 가능 여부만 null 로 저장되고 나머지는 정상 수집된다 "
        "(`warnings` 에 표시).\n\n"
        "서버에 `REFRESH_TOKEN` 이 설정돼 있으면 `X-Refresh-Token` 헤더가 필요하다 "
        "(로컬 개발처럼 비어 있으면 검사하지 않는다). 동시 호출은 서버에서 "
        "직렬화된다."
    ),
)
async def refresh_db(
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[None, Depends(_check_refresh_token)],
) -> RefreshResult:
    return await collector_service.refresh(session)
