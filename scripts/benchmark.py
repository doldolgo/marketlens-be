"""ccxt vs MarketLens 직접 호출 속도 비교.

실행:
    pip install ccxt          # 벤치마크 전용 (런타임 의존성 아님)
    python -m scripts.benchmark

두 방식 모두 "업비트 KRW-BTC + 바이낸스 BTCUSDT 호가를 가져온다" 는
동일한 작업을 수행하고, 왕복 시간을 비교한다.
"""

from __future__ import annotations

import asyncio
import statistics
import time

from app.core.http import shutdown_http_client, startup_http_client
from app.models.orderbook import MarketType
from app.models.symbol import Symbol
from app.services.market_data_service import market_data_service

ROUNDS = 10

TARGETS = [
    ("upbit", Symbol(base="BTC", quote="KRW"), MarketType.SPOT),
    ("binance", Symbol(base="BTC", quote="USDT"), MarketType.SPOT),
]


def bench_ccxt(rounds: int) -> list[float]:
    """ccxt 동기 방식: 거래소를 순차 호출한다."""
    import ccxt

    upbit = ccxt.upbit()
    binance = ccxt.binance()

    # 마켓 메타데이터 로딩은 최초 1회 비용이므로 측정 밖에서 미리 끝낸다 (ccxt 에 유리하게).
    upbit.load_markets()
    binance.load_markets()

    samples = []
    for _ in range(rounds):
        started = time.perf_counter()
        upbit.fetch_order_book("BTC/KRW", limit=10)
        binance.fetch_order_book("BTC/USDT", limit=10)
        samples.append((time.perf_counter() - started) * 1000)
    return samples


async def bench_marketlens(rounds: int) -> list[float]:
    """MarketLens 비동기 방식: 두 거래소를 동시에 호출한다."""
    await startup_http_client()

    # 커넥션 풀 워밍업 (첫 요청의 TLS 핸드셰이크를 측정에서 제외)
    await market_data_service.fetch_orderbooks(TARGETS, depth=10)

    samples = []
    for _ in range(rounds):
        started = time.perf_counter()
        await market_data_service.fetch_orderbooks(TARGETS, depth=10)
        samples.append((time.perf_counter() - started) * 1000)

    await shutdown_http_client()
    return samples


def summarize(label: str, samples: list[float]) -> None:
    print(
        f"{label:<28} "
        f"평균 {statistics.mean(samples):7.1f}ms  "
        f"중앙값 {statistics.median(samples):7.1f}ms  "
        f"최소 {min(samples):7.1f}ms  최대 {max(samples):7.1f}ms"
    )


def main() -> None:
    print(f"업비트 + 바이낸스 호가 동시 조회, {ROUNDS}회 반복\n")

    ccxt_samples = bench_ccxt(ROUNDS)
    ml_samples = asyncio.run(bench_marketlens(ROUNDS))

    summarize("ccxt (동기 · 순차)", ccxt_samples)
    summarize("MarketLens (비동기 · 동시)", ml_samples)

    speedup = statistics.median(ccxt_samples) / statistics.median(ml_samples)
    print(f"\n중앙값 기준 {speedup:.2f}배 빠름")


if __name__ == "__main__":
    main()
