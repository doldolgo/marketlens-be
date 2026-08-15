"""김프/역프 **구간(streak)** 계산 — 기록/통계 창의 요약 데이터.

``premium_archive`` 는 "언제 김프가 몇 %였는지"의 점 목록이다. 여기서 알고
싶은 것은 점이 아니라 **구간**이다 — "김프가 1% 넘게 유지된 때가 언제부터
언제까지, 몇 번, 얼마나 오래 있었는가".

계산 규칙
    1. 한 코인의 기록을 시각 순으로 세운다.
    2. 기준치(threshold) **이상**인 점만 남긴다.
    3. 살아남은 점들 중 **시각이 이어지는 것끼리** 묶으면 그것이 한 구간이다.

예 (기준치 4):

    값     0  1  3  6  29  4  31
    남김   ·  ·  ·  6  29  4  31     → 구간 1개 (6 … 31)

기준치를 5 로 올리면 4 가 탈락하면서 이어짐이 끊겨 구간이 둘로 나뉜다:

    값     0  1  3  6  29  4  31
    남김   ·  ·  ·  6  29  ·  31     → 구간 2개 (6 … 29), (31)

**방향은 따로 센다.** ``fwd``(김프)와 ``rev``(역프)는 각각 "그 방향으로 사고
팔았을 때의 수익률"이라 부호를 뒤집은 같은 값이 아니다 — 양쪽 호가 차이 때문에
둘 다 음수인 순간도 많다(실측: 기록의 상당수가 그렇다). 그래서 절댓값을 쓰지
않고 ``fwd >= 기준치`` 를 김프 구간, ``rev >= 기준치`` 를 역프 구간으로 본다.

**끊긴 구간은 이어 붙이지 않는다.** 수집이 멈췄다 재개되면 기록 사이가 몇
시간씩 벌어지는데, 그 둘을 한 구간으로 묶으면 "3시간 연속 김프"라는 없던 사실이
만들어진다. 그래서 이웃한 두 점의 간격이 ``max_gap_seconds`` 를 넘으면 구간을
끊는다 (실측 정상 간격은 약 60초).
"""

from __future__ import annotations

from dataclasses import dataclass

#: 이 초를 넘겨 벌어진 기록 사이는 "끊겼다"고 본다.
#: 정상 수집 간격(약 60초)의 10배 — 몇 회차 걸러진 정도는 이어 붙이고,
#: 수집 중단으로 생긴 큰 구멍은 끊는다.
DEFAULT_MAX_GAP_SECONDS = 600


@dataclass(frozen=True)
class Segment:
    """기준치 이상이 이어진 한 구간."""

    start_ts: int
    end_ts: int
    #: 구간에 속한 기록 수. 1 이면 한 순간만 기준치를 넘었다는 뜻이다.
    samples: int
    max_percent: float
    avg_percent: float

    @property
    def duration_seconds(self) -> int:
        """지속 시간. 기록이 하나뿐인 구간은 0 이다."""
        return self.end_ts - self.start_ts


@dataclass(frozen=True)
class StreakStats:
    """한 방향(김프 또는 역프)의 구간 요약."""

    segments: list[Segment]

    @property
    def count(self) -> int:
        return len(self.segments)

    @property
    def max_duration_seconds(self) -> int:
        return max((s.duration_seconds for s in self.segments), default=0)

    @property
    def avg_duration_seconds(self) -> float:
        if not self.segments:
            return 0.0
        return sum(s.duration_seconds for s in self.segments) / len(self.segments)

    @property
    def max_percent(self) -> float:
        return max((s.max_percent for s in self.segments), default=0.0)

    @property
    def avg_percent(self) -> float:
        """기준치를 넘은 **기록들**의 평균 (구간 평균의 평균이 아니다).

        구간마다 길이가 달라서 구간 평균을 다시 평균 내면 짧은 구간이
        과대평가된다. 기록 수로 가중해 실제 평균을 낸다.
        """
        total = sum(s.samples for s in self.segments)
        if not total:
            return 0.0
        return sum(s.avg_percent * s.samples for s in self.segments) / total


def find_segments(
    points: list[tuple[int, float]],
    threshold: float,
    *,
    max_gap_seconds: int = DEFAULT_MAX_GAP_SECONDS,
) -> StreakStats:
    """시각 순 (ts, 값) 목록에서 기준치 이상인 구간들을 뽑는다.

    Args:
        points: (epoch 초, 퍼센트) 목록. **시각 오름차순이어야 한다.**
        threshold: 기준치. 이 값 **이상**인 기록만 남는다 (초과가 아니라 이상).
        max_gap_seconds: 이 초를 넘겨 벌어진 이웃 기록 사이는 구간을 끊는다.

    Returns:
        구간 목록과 요약 통계.
    """
    segments: list[Segment] = []
    #: 지금 쌓고 있는 구간의 (시작 ts, 마지막 ts, 값 목록)
    start: int | None = None
    last_ts = 0
    values: list[float] = []

    def flush() -> None:
        if start is None or not values:
            return
        segments.append(
            Segment(
                start_ts=start,
                end_ts=last_ts,
                samples=len(values),
                max_percent=max(values),
                avg_percent=sum(values) / len(values),
            )
        )

    for ts, value in points:
        if value < threshold:
            flush()
            start, values = None, []
            continue
        if start is not None and ts - last_ts > max_gap_seconds:
            # 기록이 끊겼다 — 앞 구간을 닫고 여기서 새로 시작한다.
            flush()
            start, values = None, []
        if start is None:
            start = ts
            values = []
        values.append(value)
        last_ts = ts

    flush()
    return StreakStats(segments=segments)
