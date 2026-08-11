"""DB 갱신(refresh) 응답 모델."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ExchangeRefreshStat(BaseModel):
    """거래소 하나의 갱신 결과."""

    exchange: str = Field(..., description="거래소 ID")
    saved: int = Field(..., description="저장(UPSERT)한 코인 수")
    deleted: int = Field(..., description="이번 수집에 없어서 지운 코인 수")
    wallet_status_available: bool = Field(
        ...,
        description=(
            "입출금 가능 여부를 채웠는지. False 면 키가 없거나 조회에 실패해 "
            "deposit_enabled / withdrawal_enabled 가 null 로 저장됐다"
        ),
    )
    mode: str = Field(
        ..., description="`bulk`=전종목 일괄 조회, `per_symbol`=심볼별 조회"
    )


class KrwRateInfo(BaseModel):
    """저장한 환율 한 건."""

    exchange: str = Field(..., description="국내 거래소 ID")
    rate: float = Field(..., description="USDT 1개당 원화 가격")


class RefreshFailure(BaseModel):
    """수집하지 못한 항목."""

    exchange: str = Field(..., description="거래소 ID")
    sym: str = Field("", description="코인 심볼 (거래소 단위 실패면 빈 값)")
    error_code: str = Field(..., description="에러 코드")
    message: str = Field(..., description="에러 메시지")


class RefreshResult(BaseModel):
    """POST /refresh 응답 — DB 에 무엇이 저장됐는지."""

    snapshots: list[ExchangeRefreshStat] = Field(
        default_factory=list, description="거래소별 스냅샷 저장 결과"
    )
    krw_rates: list[KrwRateInfo] = Field(
        default_factory=list, description="저장한 KRW-USDT 환율"
    )
    total_saved: int = Field(0, description="저장한 전체 행 수")

    failures: list[RefreshFailure] = Field(
        default_factory=list, description="수집 실패 항목"
    )
    warnings: list[str] = Field(default_factory=list, description="주의 사항")

    total_calls: int = Field(0, description="이번 갱신에서 나간 거래소 HTTP 호출 수")
    fetched_at: int = Field(..., description="서버 응답 생성 시각 (epoch ms)")
    elapsed_ms: float = Field(..., description="전체 처리 시간 (ms)")
