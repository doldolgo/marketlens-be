"""여러 거래소를 동시에 호출하는 공통 헬퍼.

거래소를 순차 호출하면 왕복시간의 합이지만, ``asyncio.gather`` 로 동시에 던지면
가장 느린 하나의 왕복시간만 든다. 이것이 ccxt 대비 속도 이점의 핵심이다.

한 거래소가 실패해도 나머지 결과는 그대로 반환한다 (부분 실패 허용).
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, TypeVar

from app.core.errors import MarketLensError
from app.models.orderbook import MarketType
from app.models.symbol import Symbol

T = TypeVar("T")

#: (거래소 ID, 심볼, 시장구분)
Target = tuple[str, Symbol, MarketType]


class FanOutFailure:
    """조회에 실패한 거래소 하나."""

    __slots__ = ("exchange", "symbol", "error_code", "message")

    def __init__(self, exchange: str, symbol: str, error_code: str, message: str) -> None:
        self.exchange = exchange
        self.symbol = symbol
        self.error_code = error_code
        self.message = message


async def fan_out(
    targets: list[Target],
    fetch: Callable[[Target], Awaitable[T]],
) -> tuple[list[T], list[FanOutFailure]]:
    """모든 대상을 동시에 조회하고 (성공 목록, 실패 목록) 으로 나눠 반환한다.

    Args:
        targets: (거래소 ID, 심볼, 시장구분) 목록.
        fetch: 대상 하나를 조회하는 코루틴 함수.
    """
    results = await asyncio.gather(
        *(fetch(target) for target in targets),
        return_exceptions=True,
    )

    succeeded: list[T] = []
    failures: list[FanOutFailure] = []

    for (exchange_id, symbol, _), result in zip(targets, results, strict=True):
        if isinstance(result, MarketLensError):
            failures.append(
                FanOutFailure(exchange_id, str(symbol), result.code, result.message)
            )
        elif isinstance(result, BaseException):
            failures.append(
                FanOutFailure(
                    exchange_id,
                    str(symbol),
                    "internal_error",
                    str(result) or type(result).__name__,
                )
            )
        else:
            succeeded.append(result)

    return succeeded, failures
