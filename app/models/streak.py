"""김프/역프 구간 통계 응답 모델 (GET /history/streaks)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class StreakSegment(BaseModel):
    """기준치 이상이 이어진 한 구간."""

    start_ts: int = Field(..., description="구간 시작 시각 (epoch 초)")
    end_ts: int = Field(..., description="구간 종료 시각 (epoch 초)")
    start: str = Field(..., description="구간 시작 시각 (KST, ISO 8601)")
    end: str = Field(..., description="구간 종료 시각 (KST, ISO 8601)")
    duration_seconds: int = Field(
        ...,
        description=(
            "지속 시간 = end_ts - start_ts. 한 순간만 기준치를 넘었으면 0 이다 "
            "(samples=1)"
        ),
    )
    samples: int = Field(..., description="구간에 속한 기록 수")
    max_percent: float = Field(..., description="구간 내 최대 스프레드 %")
    avg_percent: float = Field(..., description="구간 내 평균 스프레드 %")


class StreakDirection(BaseModel):
    """한 방향(김프 또는 역프)의 구간 목록과 요약."""

    count: int = Field(..., description="구간 수 — 기준치를 넘긴 '횟수'")
    max_duration_seconds: int = Field(..., description="가장 오래 지속된 구간의 길이")
    avg_duration_seconds: float = Field(..., description="구간 평균 지속 시간")
    max_percent: float = Field(
        ..., description="기준치를 넘긴 기록 중 최대 스프레드 %. 없으면 0"
    )
    avg_percent: float = Field(
        ...,
        description=(
            "기준치를 넘긴 **기록들**의 평균 스프레드 % (기록 수로 가중). "
            "구간 평균을 다시 평균 내면 짧은 구간이 과대평가돼 그렇게 하지 않는다"
        ),
    )
    segments: list[StreakSegment] = Field(
        default_factory=list, description="구간 목록 (시각 오름차순)"
    )


class OverallStats(BaseModel):
    """조회 구간 **전체** 요약 — 기준치와 무관하다.

    ``kimp`` / ``reverse`` 의 통계는 기준치를 넘긴 기록만 본 것이라, 기준치를
    올리면 표본이 줄어 값이 함께 움직인다. 이 블록은 기준치를 적용하기 전의
    원본 기록 전체를 본다 — "기준치를 한 번도 못 넘었지만 최고 몇 %까지
    갔는가" 같은 질문에 답한다.
    """

    max_kimp_percent: float = Field(
        ..., description="구간 전체 기록 중 최대 김프 % (기준치 무관). 음수일 수 있다"
    )
    avg_kimp_percent: float = Field(
        ...,
        description=(
            "구간 전체 기록의 평균 김프 %. 기준치 미만·음수 기록까지 모두 포함한 "
            "산술 평균이라 kimp.avg_percent 보다 낮게 나오는 것이 정상이다"
        ),
    )
    max_reverse_percent: float = Field(
        ..., description="구간 전체 기록 중 최대 역프 % (기준치 무관)"
    )
    avg_reverse_percent: float = Field(
        ..., description="구간 전체 기록의 평균 역프 %"
    )

    max_duration_seconds: int = Field(
        ...,
        description=(
            "김프·역프를 **합쳐** 가장 오래 지속된 구간의 길이. "
            "구간은 기준치로 정의되므로 이 둘만은 기준치의 영향을 받는다"
        ),
    )
    avg_duration_seconds: float = Field(
        ..., description="김프·역프를 합친 구간들의 평균 지속 시간"
    )
    segment_count: int = Field(
        ..., description="김프 구간 수 + 역프 구간 수"
    )


class StreakResponse(BaseModel):
    """GET /history/streaks 응답."""

    base: str = Field(..., description="코인 심볼")
    dom: str = Field(..., description="국내 거래소 ID")
    fx: str = Field(..., description="해외 거래소 ID")
    threshold_percent: float = Field(
        ..., description="입력한 기준치 — 이 값 **이상**인 기록만 구간에 든다"
    )
    max_gap_seconds: int = Field(
        ..., description="이 초를 넘겨 기록이 벌어지면 구간을 끊었다"
    )

    start_ts: int = Field(..., description="조회 구간 시작 (epoch 초)")
    end_ts: int = Field(..., description="조회 구간 종료 (epoch 초)")
    scanned: int = Field(..., description="조회 구간에서 훑은 기록 수")

    kimp: StreakDirection = Field(
        ..., description="김프 구간 — 해외에서 사서 국내에 팔 때 수익이 난 구간"
    )
    reverse: StreakDirection = Field(
        ..., description="역프 구간 — 국내에서 사서 해외에 팔 때 수익이 난 구간"
    )
    overall: OverallStats = Field(
        ..., description="조회 구간 전체 요약 (기준치를 적용하기 전 기준)"
    )

    last_updated_ts: int | None = Field(
        None,
        description=(
            "이 코인의 **가장 최근 기록 시각** (epoch 초). 조회 구간 밖이어도 "
            "DB 전체 기준으로 알려준다 — 데이터가 얼마나 신선한지 판단용. "
            "기록이 하나도 없으면 null"
        ),
    )
    last_updated: str | None = Field(
        None, description="가장 최근 기록 시각 (KST, ISO 8601)"
    )
    fetched_at: int = Field(..., description="서버 응답 생성 시각 (epoch ms)")
