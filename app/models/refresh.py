"""DB 갱신(refresh) 응답 모델."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ExchangeRefreshStat(BaseModel):
    """거래소 하나의 갱신 결과."""

    exchange: str = Field(..., description="거래소 ID")
    saved: int = Field(
        ...,
        description=(
            "저장(UPSERT)한 코인 수. 이번 수집에 빠진 코인도 지우지 않는다 — "
            "코인을 찾아 갱신만 하며, 낡은 행은 updated_at 으로 판별한다"
        ),
    )
    wallet_status_available: bool = Field(
        ...,
        description=(
            "입출금 가능 여부를 채웠는지. False 면 키가 없거나 조회에 실패해 "
            "deposit_enabled / withdrawal_enabled 가 null 로 저장됐다"
        ),
    )


class UsdKrwRateInfo(BaseModel):
    """저장한 통일 환율 (하나은행 고시 USD/KRW 매매기준율)."""

    rate: float = Field(..., description="USD 1달러당 원화 (매매기준율)")
    source_time: int = Field(..., description="은행 고시 시각 (epoch 초)")
    round_no: int = Field(..., description="당일 고시 회차")


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
    usdkrw: UsdKrwRateInfo | None = Field(
        None,
        description=(
            "저장한 통일 환율 (하나은행 USD/KRW 매매기준율). "
            "이번 수집에 실패했으면 null — 계산은 DB 의 마지막 환율로 계속된다"
        ),
    )
    total_saved: int = Field(0, description="저장한 전체 행 수")
    deleted: int = Field(
        0,
        description=(
            "짝을 잃어 market_snapshots 에서 지운 행 수 — 국내·해외 한쪽에만 "
            "남아 김프를 계산할 수 없게 된 코인. 지우기 전 마지막 김프를 "
            "premium_archive 에 남긴다"
        ),
    )
    archived: int = Field(
        0,
        description=(
            "이번 회차에 김프/역프 기록(premium_archive)으로 남긴 행 수 — "
            "(국내 거래소 × 코인) 조합마다 한 줄"
        ),
    )

    failures: list[RefreshFailure] = Field(
        default_factory=list, description="수집 실패 항목"
    )
    warnings: list[str] = Field(default_factory=list, description="주의 사항")

    total_calls: int = Field(0, description="이번 갱신에서 나간 거래소 HTTP 호출 수")
    fetched_at: int = Field(..., description="서버 응답 생성 시각 (epoch ms)")
    elapsed_ms: float = Field(..., description="전체 처리 시간 (ms)")
