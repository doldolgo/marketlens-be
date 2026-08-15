"""바이낸스 1초봉 수집기 — 가격 변동 이력의 해외(USDT) 축.

바이낸스 스팟 klines(``/api/v3/klines``, interval=1s)를 쓴다.

특성 (2026-08-13 실호출로 검증)
    - 업비트와 달리 **모든 초에 캔들이 존재**한다 (밀집). 체결이 없던 초는
      직전 종가가 그대로 이어진다. 그래서 "종가가 직전과 달라진 초"만 남기면
      가격 경로를 잃지 않으면서 포인트 수가 절반 이하로 준다
      (실측: BTCUSDT 하루 86,400초 중 41.8%만 변동).
    - 요청당 최대 1000개. ``startTime``(ms) 로 과거→현재 방향 페이지네이션.
      다음 페이지 startTime = 마지막 캔들 closeTime + 1.
    - 이력은 상장 시점(2017년)까지 전부 있다. 3개월은 여유.
    - 가중치 한도(6000/min)에서 klines 는 호출당 2 — 사실상 제한 없음.

가격 문자열("63831.99000000")을 Decimal 로 파싱한다. 변동 판정은
값 비교이므로 뒤 0 개수와 무관하게 정확하다.

테스트 예시 (브라우저에 붙여넣으면 JSON 이 바로 보인다):
    최신 10개:
      https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1s&limit=10
    과거 시점부터 (startTime 은 epoch ms):
      https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1s&startTime=1786492800000&limit=10
    ※ api.binance.com 이 차단된 망에서는 공식 시세 미러로 같은 데이터를 볼 수 있다:
      https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=1s&limit=10
      (BINANCE_SPOT_BASE_URL 설정으로 갈아끼울 수 있다)
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import httpx

from app.core.config import settings
from app.core.http import get_client, record_call

#: 요청당 최대 캔들 수 (바이낸스 고정 한도)
MAX_LIMIT = 1000

#: 요청 간격 (초). 가중치 여유가 크므로 짧아도 된다.
DEFAULT_PACE = 0.1

RETRIES = 3
RETRY_WAIT = 1.0


async def _get_klines(symbol: str, *, start_ms: int, limit: int) -> list[list]:
    """1초봉 한 페이지 — start_ms(epoch ms) 부터 limit 개."""
    client = get_client()
    last_error: Exception = RuntimeError("바이낸스 1초봉 조회 실패")
    for attempt in range(RETRIES):
        try:
            record_call("binance")
            response = await client.get(
                f"{settings.binance_spot_base_url}/api/v3/klines",
                params={
                    "symbol": symbol,
                    "interval": "1s",
                    "startTime": start_ms,
                    "limit": limit,
                },
            )
            if response.status_code in (418, 429):
                # 418 은 밴 경고 — 더 길게 쉰다.
                last_error = RuntimeError(
                    f"바이낸스 레이트리밋({response.status_code})"
                )
                await asyncio.sleep(RETRY_WAIT * (attempt + 1) * 2)
                continue
            if response.status_code >= 500:
                last_error = RuntimeError(
                    f"바이낸스 HTTP {response.status_code}"
                )
                await asyncio.sleep(RETRY_WAIT * (attempt + 1))
                continue
            response.raise_for_status()
            return response.json()
        except httpx.TransportError as exc:
            last_error = exc  # 타임아웃·커넥션 오류 — 재시도
            await asyncio.sleep(RETRY_WAIT * (attempt + 1))
    raise last_error


async def fetch_1s_range(
    base: str,
    start_ts: int,
    end_ts: int,
    *,
    pace: float = DEFAULT_PACE,
) -> list[tuple[int, Decimal]]:
    """[start_ts, end_ts) 구간의 1초봉 종가를 (epoch 초, 종가) 오름차순으로.

    반환값은 **모든 초**를 담는다 (밀집). 변동만 남기는 축약은 호출자
    (아카이브 서비스) 몫이다 — 수집과 축약을 분리해 두면 축약 규칙이
    바뀌어도 수집기는 그대로다.
    """
    symbol = f"{base.upper()}USDT"
    out: list[tuple[int, Decimal]] = []
    cursor_ms = start_ts * 1000
    end_ms = end_ts * 1000

    while cursor_ms < end_ms:
        klines = await _get_klines(symbol, start_ms=cursor_ms, limit=MAX_LIMIT)
        if not klines:
            break  # 상장 이전 구간이거나 아직 데이터가 없는 미래

        for k in klines:
            open_ms = int(k[0])
            if open_ms >= end_ms:
                break
            # k[4] = 종가 (문자열). str → Decimal 은 값 그대로다.
            out.append((open_ms // 1000, Decimal(k[4])))

        last_close_ms = int(klines[-1][6])
        next_ms = last_close_ms + 1
        if next_ms <= cursor_ms:
            break  # 방어: 커서가 전진하지 않으면 중단
        cursor_ms = next_ms
        await asyncio.sleep(pace)

    return out
