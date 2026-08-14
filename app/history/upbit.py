"""업비트 초봉 수집기 — 가격 변동 이력의 국내(KRW) 축.

업비트 공개 API 의 초봉(``/v1/candles/seconds``)을 쓴다.

특성 (2026-08-13 실호출로 검증)
    - 체결이 있었던 초에만 캔들이 존재한다 (희소). 즉 캔들 하나가
      "이 초에 거래가 있었고 마지막 체결가는 얼마" 라는 이벤트다.
    - 요청당 최대 200개. ``to`` 파라미터(exclusive)로 과거 방향 페이지네이션.
    - 보관 기간은 **롤링 약 3개월**. 그 밖은 에러가 아니라 빈 배열이 온다 —
      루프 종료 조건으로 처리해야 한다.
    - 레이트리밋: candles 그룹 10 req/s, 600 req/min (IP 기준).

가격은 JSON 을 Decimal 로 파싱한다. float 을 거치면 0.0001 원 단위 틱을 가진
저가 코인에서 반올림이 생길 수 있다 — 무손실 저장의 전제가 깨진다.

테스트 예시 (브라우저에 붙여넣으면 JSON 이 바로 보인다):
    최신 10개:
      https://api.upbit.com/v1/candles/seconds?market=KRW-BTC&count=10
    과거 시점부터 (to 는 exclusive, UTC):
      https://api.upbit.com/v1/candles/seconds?market=KRW-BTC&count=200&to=2026-08-01T00:00:00Z
    ※ 일부 통신사 콘텐츠 필터 망에서는 api.upbit.com 이 차단된다 — EC2/LTE 에선 정상.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal

import httpx

from app.core.config import settings
from app.core.http import get_client, record_call

#: 요청당 최대 캔들 수 (업비트 고정 한도 — 초과분은 조용히 잘린다)
MAX_COUNT = 200

#: 요청 간격 (초). 10 req/s 한도의 절반 정도로 여유를 둔다 —
#: 같은 IP 의 라이브 refresh 가 같은 한도를 나눠 쓰기 때문.
DEFAULT_PACE = 0.2

#: 429 등 일시 오류 시 재시도 횟수와 대기 시간(초)
RETRIES = 3
RETRY_WAIT = 1.0


def _candle_ts(candle: dict) -> int:
    """캔들의 UTC 시각 문자열("2026-08-13T10:59:34")을 epoch 초로 바꾼다."""
    dt = datetime.fromisoformat(candle["candle_date_time_utc"])
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


async def _get_candles(market: str, *, count: int, to_ts: int | None) -> list[dict]:
    """초봉 한 페이지를 가져온다. to_ts(epoch 초, exclusive) 이전 count 개."""
    params: dict[str, str | int] = {"market": market, "count": count}
    if to_ts is not None:
        params["to"] = (
            datetime.fromtimestamp(to_ts, tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S"
            )
            + "Z"
        )

    client = get_client()
    last_error: Exception = RuntimeError("업비트 초봉 조회 실패")
    for attempt in range(RETRIES):
        try:
            record_call("upbit")
            response = await client.get(
                f"{settings.upbit_base_url}/v1/candles/seconds", params=params
            )
            if response.status_code == 429 or response.status_code >= 500:
                # 레이트리밋·서버 오류는 일시적일 가능성이 크다 — 쉬고 재시도.
                last_error = RuntimeError(
                    f"업비트 HTTP {response.status_code}"
                )
                await asyncio.sleep(RETRY_WAIT * (attempt + 1))
                continue
            response.raise_for_status()  # 4xx 는 재시도해도 소용없다 — 즉시 예외
            # parse_float=Decimal — 가격을 float 로 만들지 않는 것이 무손실의 핵심.
            return json.loads(response.text, parse_float=Decimal)
        except httpx.TransportError as exc:
            # 타임아웃·커넥션 오류 — 몇 시간짜리 백필이 일시 장애 한 번에
            # 죽지 않도록 재시도한다.
            last_error = exc
            await asyncio.sleep(RETRY_WAIT * (attempt + 1))
    raise last_error


async def fetch_seconds_range(
    base: str,
    start_ts: int,
    end_ts: int,
    *,
    pace: float = DEFAULT_PACE,
) -> list[tuple[int, Decimal]]:
    """[start_ts, end_ts) 구간의 초봉을 전부 모아 (epoch 초, 체결가) 오름차순으로.

    end_ts 쪽에서 과거로 200개씩 걸어 내려간다. 다음 페이지의 ``to`` 는
    직전 페이지에서 가장 오래된 캔들 시각 — ``to`` 가 exclusive 라 그
    캔들 직전부터 이어진다.

    3개월 보관 한계를 넘어가면 업비트가 빈 배열을 주므로 거기서 멈춘다.
    (그래서 start_ts 가 보관 한계보다 과거면 얻은 만큼만 반환된다)
    """
    market = f"KRW-{base.upper()}"
    collected: dict[int, Decimal] = {}
    cursor = end_ts

    while cursor > start_ts:
        candles = await _get_candles(market, count=MAX_COUNT, to_ts=cursor)
        if not candles:
            break  # 3개월 보관 한계 또는 상장 이전 — 더 과거는 없다

        oldest = None
        for candle in candles:  # 응답은 최신 → 과거 순
            ts = _candle_ts(candle)
            oldest = ts if oldest is None else min(oldest, ts)
            if start_ts <= ts < end_ts:
                collected[ts] = candle["trade_price"]

        if oldest is None or oldest >= cursor:
            break  # 방어: 커서가 전진하지 않으면 무한 루프를 막는다
        cursor = oldest
        await asyncio.sleep(pace)

    return sorted(collected.items())
