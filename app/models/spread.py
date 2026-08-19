"""스프레드 테이블 모델 — FE 의 SpreadRow 계약과 1:1 로 맞춘다.

FE `src/data/types.ts` 의 SpreadRow 가 원본 계약이다.

    { sym, dom, fx, fwd, rev, usd, spark, status, age, liqDom, liqFx }

(국내 거래소 × 해외 거래소 × 코인) 페어 하나가 한 행이며, 한 행에
**김프(fwd)와 역프(rev)를 함께** 담는다. 가격 기준은 다른 프리미엄 API 와
동일하게 체결되는 쪽 호가다 — 살 때 매도호가(ask), 팔 때 매수호가(bid).

데이터는 전부 DB 스냅샷에서 나온다. 거래소 직접 호출은 없다.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class FeedStatus(str, Enum):
    """페어 데이터의 수신 상태."""

    #: 정상 — 스냅샷이 충분히 신선하다
    OK = "ok"
    #: 오래됨 — 마지막 갱신 후 ``SPREAD_STALE_SECONDS`` 를 넘겼다
    STALE = "stale"
    #: 계산 불가 — 저장된 호가가 비어 있어 값을 만들 수 없다
    FAIL = "fail"


class SpreadRow(BaseModel):
    """국내 × 해외 김프/역프 페어 한 행 (FE SpreadRow 와 동일 형태)."""

    model_config = ConfigDict(populate_by_name=True)

    sym: str = Field(..., description="코인 심볼 (예: BTC)")
    dom: str = Field(..., description="국내 거래소 ID (upbit / bithumb)")
    fx: str = Field(..., description="해외 거래소 ID (예: binance)")

    fwd: float = Field(
        ...,
        description=(
            "순방향 김프 (%) — 해외 매수(ask) → 국내 매도(bid). "
            "status=fail 이면 0"
        ),
    )
    rev: float = Field(
        ...,
        description=(
            "역방향 (%) — 국내 매수(ask) → 해외 매도(bid). status=fail 이면 0"
        ),
    )
    usd: float = Field(
        ..., description="해외 USD(T) 가격 — 마지막 체결가. status=fail 이면 0"
    )

    spark: list[float] = Field(
        default_factory=list,
        description="프리미엄 추이 스파크라인. 이력 저장 전까지는 항상 빈 배열",
    )

    status: FeedStatus = Field(..., description="ok / stale / fail")
    age: float = Field(
        ..., description="스냅샷 마지막 갱신 후 경과 초 (양측 중 오래된 쪽)"
    )

    liq_dom: float = Field(
        ...,
        alias="liqDom",
        description=(
            "국내 최우선 호가 유동성 (USD 환산) — 슬리피지 추정용. "
            "매수·매도 양쪽 중 작은 쪽"
        ),
    )
    liq_fx: float = Field(
        ...,
        alias="liqFx",
        description="해외 최우선 호가 유동성 (USDT) — 매수·매도 양쪽 중 작은 쪽",
    )


class SpreadsResult(BaseModel):
    """GET /spreads 응답."""

    rate: float = Field(
        ...,
        description=(
            "기준 USDT/KRW 환율 (기준 국내 거래소 저장값). "
            "usd × rate 로 원화 환산에 쓴다. 계산 자체는 각 행의 국내 거래소 "
            "자기 환율을 쓴다"
        ),
    )
    rows: list[SpreadRow] = Field(
        default_factory=list,
        description="(국내 × 해외 × 코인) 페어 전체. sym → dom → fx 순 정렬",
    )

    data_received_at: int | None = Field(
        None,
        description=(
            "이 응답의 데이터를 **거래소에서 받은** 시각 (epoch ms). "
            "응답을 만든 시각(fetched_at) · 코인별 스냅샷 갱신 시각"
            "(data_updated_at)과 뜻이 다르다"
        ),
    )
    fetched_at: int = Field(..., description="서버 응답 생성 시각 (epoch ms)")
    elapsed_ms: float = Field(..., description="전체 처리 시간 (ms)")
