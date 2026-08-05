"""거래소 메타데이터 라우터."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.exchanges.registry import all_exchanges

router = APIRouter(prefix="/exchanges", tags=["exchanges"])


class ExchangeInfo(BaseModel):
    """등록된 거래소 정보."""

    id: str = Field(..., description="거래소 ID (다른 엔드포인트에서 사용)")
    name: str = Field(..., description="거래소 이름")
    default_quote: str = Field(..., description="비교 시 사용하는 기본 결제 통화")
    quote_currencies: list[str] = Field(..., description="지원하는 결제 통화")
    market_types: list[str] = Field(..., description="지원하는 시장 구분")


@router.get("", response_model=list[ExchangeInfo], summary="지원 거래소 목록")
async def list_exchanges() -> list[ExchangeInfo]:
    return [
        ExchangeInfo(
            id=exchange.id,
            name=exchange.name,
            default_quote=exchange.default_quote,
            quote_currencies=sorted(exchange.quote_currencies),
            market_types=sorted(mt.value for mt in exchange.supported_market_types),
        )
        for exchange in all_exchanges()
    ]
