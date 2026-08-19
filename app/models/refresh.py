"""DB 갱신(refresh) 응답 모델."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ExchangeRefreshStat(BaseModel):
    """거래소 하나의 갱신 결과."""

    exchange: str = Field(..., description="거래소 ID")
    saved: int = Field(
        ...,
        description=(
            "이번 사이클이 **메모리에 적재한** 코인 수. DB 쓰기는 이 호출이 "
            "하지 않는다 — 1분 주기 저장 루프가 따로 내린다"
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
    """이번에 관측한 한 국내 거래소의 KRW-USDT 환율."""

    exchange: str = Field(..., description="국내 거래소 ID (upbit / bithumb)")
    ask: float = Field(..., description="KRW-USDT 최우선 매도호가 — 김프 계산에 쓴다")
    bid: float = Field(..., description="KRW-USDT 최우선 매수호가 — 역프 계산에 쓴다")


class RefreshFailure(BaseModel):
    """수집하지 못한 항목."""

    exchange: str = Field(..., description="거래소 ID")
    sym: str = Field("", description="코인 심볼 (거래소 단위 실패면 빈 값)")
    error_code: str = Field(..., description="에러 코드")
    message: str = Field(..., description="에러 메시지")


class RefreshResult(BaseModel):
    """POST /refresh 응답 — 메모리에 무엇이 적재됐는지.

    이 호출은 DB 를 건드리지 않는다. ``market_snapshots`` / ``premium_archive``
    저장은 ``PERSIST_INTERVAL_SECONDS`` 주기의 별도 루프가 담당한다.
    """

    snapshots: list[ExchangeRefreshStat] = Field(
        default_factory=list, description="거래소별 스냅샷 저장 결과"
    )
    usdkrw: list[UsdKrwRateInfo] = Field(
        default_factory=list,
        description=(
            "이번에 관측한 거래소별 KRW-USDT 환율. 어떤 거래소의 USDT 호가를 "
            "못 받았으면 그 거래소는 빠진다 — 계산은 직전 환율로 계속된다"
        ),
    )
    total_saved: int = Field(0, description="메모리에 적재한 전체 행 수")

    failures: list[RefreshFailure] = Field(
        default_factory=list, description="수집 실패 항목"
    )
    warnings: list[str] = Field(default_factory=list, description="주의 사항")

    total_calls: int = Field(0, description="이번 갱신에서 나간 거래소 HTTP 호출 수")
    fetched_at: int = Field(..., description="서버 응답 생성 시각 (epoch ms)")
    elapsed_ms: float = Field(..., description="전체 처리 시간 (ms)")
