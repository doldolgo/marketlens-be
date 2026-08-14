"""MarketLens 백엔드 진입점."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
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
from app.db.database import dispose_engine, init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 공용 HTTP 커넥션 풀과 DB 엔진을 앱 수명과 함께 관리한다.
    await startup_http_client()
    await init_db()
    yield
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
