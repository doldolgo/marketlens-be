"""MarketLens 백엔드 진입점."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import arbitrage, compare, exchanges, health, orderbook, premium
from app.core.config import settings
from app.core.errors import MarketLensError
from app.core.http import shutdown_http_client, startup_http_client


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 공용 HTTP 커넥션 풀을 앱 수명과 함께 관리한다.
    await startup_http_client()
    yield
    await shutdown_http_client()


app = FastAPI(
    title=settings.app_name,
    version=settings.version,
    description=(
        "거래소 공개 API 를 직접 호출해 호가를 수집하고, 거래소 간 가격차를 계산하는 백엔드.\n\n"
        "ccxt 라이브러리를 거치지 않고 원본 REST 엔드포인트를 비동기로 동시 호출한다."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.exception_handler(MarketLensError)
async def marketlens_error_handler(request: Request, exc: MarketLensError) -> JSONResponse:
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
app.include_router(exchanges.router)
app.include_router(orderbook.router)
app.include_router(compare.router)
app.include_router(premium.router)
app.include_router(arbitrage.router)
