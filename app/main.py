"""MarketLens 백엔드 진입점."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import (
    arbitrage,
    compare,
    exchanges,
    history,
    rate,
    health,
    matrix,
    orderbook,
    premium,
    refresh,
    slippage,
    spreads,
)
from app.core.config import settings
from app.core.errors import MarketLensError
from app.core.http import shutdown_http_client, startup_http_client
from app.db.database import dispose_engine, get_session_factory, init_db
from app.services.collector_service import collector_service


# uvicorn 은 자기 로거(uvicorn.*)만 설정한다. 이걸 넣지 않으면 app.* 로거의
# 출력이 어디에도 나가지 않아, 수집기가 남기는 깊이 선정 결과와 실패 로그를
# 운영 중에 볼 수 없다.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)

logger = logging.getLogger(__name__)


async def _collect_loop() -> None:
    """수집 사이클을 앱 안에서 돌린다.

    crontab 은 최소 단위가 1분이라 1초 주기를 만들 수 없다. 게다가 기존 방식
    (``curl ... > /dev/null``)은 실패를 전부 버려서, 실제로 8시간 결측이
    났는데도 아무도 알지 못했다.

    ⚠️ 이 루프는 **프로세스 안**에 있다. uvicorn 을 ``--workers 2`` 이상으로
    띄우면 워커마다 루프가 돌아 중복 수집이 된다. 현재 Dockerfile 은 워커
    옵션이 없어 단일 프로세스이므로 안전하다 — 워커를 늘릴 거라면 수집을
    별도 프로세스로 떼거나 프로세스 간 락을 둬야 한다.
    """
    backoff = 1.0
    consecutive_failures = 0
    while True:
        try:
            async with get_session_factory()() as session:
                await collector_service.refresh(session)
            backoff = 1.0
            consecutive_failures = 0
        except asyncio.CancelledError:
            raise
        except Exception:
            consecutive_failures += 1
            logger.exception("수집 사이클 실패 (연속 %d회)", consecutive_failures)
            # 거래소·DB 장애가 길어질 때 같은 실패를 초당 한 번씩 반복하지 않는다.
            backoff = min(backoff * 2, 60.0)
            await asyncio.sleep(backoff)
            continue
        # 고정 간격이 아니라 **끝난 뒤** 대기한다 — 사이클이 주기보다 길어져도
        # 다음 사이클과 겹치지 않는다.
        await asyncio.sleep(settings.collect_interval_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 공용 HTTP 커넥션 풀과 DB 엔진을 앱 수명과 함께 관리한다.
    await startup_http_client()
    await init_db()
    collect_task = asyncio.create_task(_collect_loop())
    try:
        yield
    finally:
        collect_task.cancel()
        try:
            await collect_task
        except asyncio.CancelledError:
            pass
        await shutdown_http_client()
        await dispose_engine()


app = FastAPI(
    title=settings.app_name,
    version=settings.version,
    description=(
        "거래소 간 가격차(김프·역프)를 계산하는 백엔드.\n\n"
        "데이터 흐름은 두 갈래다.\n\n"
        "- **수집** — `POST /refresh` 가 거래소 공개 API 를 비동기로 동시 호출해 "
        "시세·호가·입출금 상태를 PostgreSQL 에 저장한다.\n"
        "- **조회** — 그 외 모든 API 는 거래소를 직접 부르지 않고 DB 를 읽어 계산한다."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
# 벌크 이력 응답(/history/streaks/bulk)이 코인 수백 개 × 구간 목록이라 수 MB 가
# 될 수 있다 — 반복 구조의 JSON 이라 gzip 으로 한 자릿수 %까지 줄어든다.
app.add_middleware(GZipMiddleware, minimum_size=8_192)


@app.exception_handler(MarketLensError)
async def marketlens_error_handler(
    request: Request, exc: MarketLensError
) -> JSONResponse:
    """도메인 예외를 일관된 JSON 에러 응답으로 변환한다."""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "detail": exc.detail,
            }
        },
    )


app.include_router(health.router)
app.include_router(refresh.router)
app.include_router(exchanges.router)
app.include_router(rate.router)
app.include_router(orderbook.router)
app.include_router(compare.router)
app.include_router(premium.router)
app.include_router(spreads.router)
app.include_router(slippage.router)
app.include_router(matrix.router)
app.include_router(arbitrage.router)
app.include_router(history.router)
