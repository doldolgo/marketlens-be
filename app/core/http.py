"""공용 비동기 HTTP 클라이언트.

ccxt 가 느린 주된 이유는 (1) 요청마다 마켓 메타데이터를 로드하고
(2) 동기 방식이라 거래소를 순차 호출하며 (3) 커넥션을 재사용하지 않는 데 있다.
여기서는 프로세스 수명 동안 살아있는 단일 ``httpx.AsyncClient`` 를 공유해
keep-alive 커넥션을 재사용하고, 여러 거래소를 동시에 호출한다.
"""

from __future__ import annotations

from collections import Counter

import httpx

from app.core.config import settings

_client: httpx.AsyncClient | None = None


#: 거래소별 누적 API 호출 수. 응답에 "거래소별 호출 횟수" 를 정직하게 싣기 위한 계측용.
#: ``BaseExchange._get_json`` 에서 증가시킨다 — 거래소 API 호출이 전부 그곳을 지난다.
_call_counts: Counter[str] = Counter()


def record_call(exchange_id: str) -> None:
    """거래소 API 호출 1회를 기록한다."""
    _call_counts[exchange_id] += 1


def request_counts() -> Counter[str]:
    """지금까지의 거래소별 누적 호출 수 사본."""
    return _call_counts.copy()


def create_client() -> httpx.AsyncClient:
    """설정값이 적용된 AsyncClient 를 생성한다."""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(
            settings.http_timeout, connect=settings.http_connect_timeout
        ),
        limits=httpx.Limits(
            max_connections=settings.http_max_connections,
            max_keepalive_connections=settings.http_max_keepalive,
        ),
        headers={"User-Agent": f"marketlens-be/{settings.version}"},
        http2=False,
    )


async def startup_http_client() -> None:
    """앱 기동 시 공용 클라이언트를 만든다."""
    global _client
    if _client is None:
        _client = create_client()


async def shutdown_http_client() -> None:
    """앱 종료 시 커넥션 풀을 정리한다."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def get_client() -> httpx.AsyncClient:
    """공용 클라이언트를 반환한다. 없으면 지연 생성한다 (테스트/스크립트 용도)."""
    global _client
    if _client is None:
        _client = create_client()
    return _client
