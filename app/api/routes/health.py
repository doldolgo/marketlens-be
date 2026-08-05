"""헬스체크 라우터."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.core.config import settings

router = APIRouter(tags=["system"])


class HealthResponse(BaseModel):
    status: str = Field(..., description="서비스 상태")
    version: str = Field(..., description="애플리케이션 버전")


@router.get("/health", response_model=HealthResponse, summary="헬스체크")
async def health() -> HealthResponse:
    return HealthResponse(status="ok", version=settings.version)
