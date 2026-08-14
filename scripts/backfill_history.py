"""가격 변동 이력 3개월 백필 스크립트.

업비트 초봉 · 바이낸스 1초봉 · 하나은행 고시환율의 과거 데이터를 받아
변동 이벤트만 남기고 압축 청크로 저장한다. 한 번 실행해두면 이후는
``POST /history/sync`` (1분 crontab) 가 이어서 쌓는다.

사용 예 (EC2 의 marketlens-be 디렉토리에서):

    python -m scripts.backfill_history --bases BTC
    python -m scripts.backfill_history --bases BTC,ETH --days 92 --skip-fx
    python -m scripts.backfill_history --bases BTC --fx-stride 5   # 환율 5회차 간격

동작 원칙
    - **하루(UTC) 단위로 진행하고 즉시 커밋**한다. 중단돼도 다시 실행하면
      이미 청크가 있는 날은 건너뛰므로 이어서 진행된다 (재개 가능).
    - 업비트 초봉은 롤링 3개월만 보관되므로 --days 를 더 줘도 얻는 만큼만
      저장된다. 바이낸스는 전체 이력이 있어 제한이 없다.
    - 환율은 하루 1,300~2,000회 고시라 전체 백필은 요청이 많다 (~15만 회,
      기본 속도로 하룻밤). --fx-stride N 으로 N회차 간격 샘플링하면 그만큼
      빨라진다 — 10분 간격(stride≈15)까지는 김프 오차 기여가 실측 수 bp 다.

주의: 업비트 레이트리밋(10 req/s)은 라이브 refresh 와 공유된다. 운영 중인
서버에서 돌릴 때는 기본 pace 를 유지할 것.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app.core.http import shutdown_http_client
from app.db.database import dispose_engine, get_session_factory, init_db
from app.history import binance as binance_history
from app.history import codec, hana, service, store
from app.history import upbit as upbit_history

KST = timezone(timedelta(hours=9))


def _day_bounds(day: date) -> tuple[int, int]:
    """UTC 날짜 하나의 [시작, 끝) epoch 초."""
    start = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())
    return start, start + 86_400


def _log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


async def _advance_cursor(
    session, exchange: str, base: str, last_ts: int, last_price: Decimal
) -> None:
    """커서를 앞으로만 전진시킨다 — sync 가 이미 더 나갔다면 건드리지 않는다."""
    cursor = await store.get_cursor(session, exchange, base)
    if cursor is None or cursor.last_ts < last_ts:
        await store.set_cursor(session, exchange, base, last_ts, str(last_price))


async def backfill_coin_exchange(
    session_factory,
    exchange: str,
    base: str,
    days: list[date],
    pace: float,
) -> None:
    """한 (거래소 × 코인) 시리즈를 과거 → 현재 방향으로 하루씩 백필한다."""
    async with session_factory() as session:
        done_days = await store.get_price_chunk_days(session, exchange, base)

    #: 직전 날의 마지막 관측 가격 — "변동" 판정의 씨앗. 재개 시 기존 청크의
    #: last_price 로 복원된다.
    prev_price: Decimal | None = None
    last_observed: tuple[int, Decimal] | None = None

    for day in days:
        start_ts, end_ts = _day_bounds(day)

        if day in done_days:
            # 이미 백필된 날 — 씨앗만 그 청크의 마지막 가격으로 갱신하고 통과.
            async with session_factory() as session:
                chunks = await store.get_price_chunks(session, exchange, base, day, day)
            if chunks:
                prev_price = codec.from_scaled(
                    chunks[0].last_price, chunks[0].price_scale
                )
                last_observed = (chunks[0].last_ts, prev_price)
            continue

        # 하루치 관측을 받아온다 (업비트=체결 초만, 바이낸스=모든 초).
        # 수집기 내부 재시도로도 안 되는 장애면 이 날은 건너뛰고 계속한다 —
        # 빠진 날은 스크립트를 다시 돌리면 (done_days 검사로) 그 날만 채워진다.
        try:
            if exchange == "upbit":
                observed = await upbit_history.fetch_seconds_range(
                    base, start_ts, end_ts, pace=pace
                )
            else:
                observed = await binance_history.fetch_1s_range(
                    base, start_ts, end_ts, pace=pace
                )
        except Exception as exc:  # noqa: BLE001 — 밤샘 실행 보호
            _log(f"{exchange}:{base} {day} — 실패, 건너뜀: {type(exc).__name__}: {exc}")
            await asyncio.sleep(10)
            continue

        if not observed:
            _log(f"{exchange}:{base} {day} — 데이터 없음 (보관 한계 밖?)")
            continue

        changes = service.keep_changes(observed, prev_price)
        prev_price = observed[-1][1]
        last_observed = observed[-1]

        if changes:
            async with session_factory() as session:
                # 스테이징에 넣고 곧바로 그 날을 팩킹한다 — sync 와 같은 경로라
                # 코덱 round-trip 검증까지 동일하게 거친다.
                await store.add_price_points(
                    session, exchange, base, [(ts, str(p)) for ts, p in changes]
                )
                packed = await service.pack_price_days(
                    session, exchange, base, now_ts=end_ts
                )
                await session.commit()
            _log(
                f"{exchange}:{base} {day} — 관측 {len(observed):,}건 → "
                f"변동 {len(changes):,}건 저장 (청크 {packed}개)"
            )
        else:
            _log(f"{exchange}:{base} {day} — 가격 변동 없음")

    if last_observed is not None:
        async with session_factory() as session:
            await _advance_cursor(
                session, exchange, base, last_observed[0], last_observed[1]
            )
            await session.commit()


async def backfill_fx(
    session_factory,
    first_day: date,
    stride: int,
    pace: float,
) -> None:
    """하나은행 고시환율을 기준일 단위로 백필한다.

    기준일의 고시가 다음날 아침(KST)까지 이어지므로, 이벤트는 실제 고시
    시각의 UTC 날짜로 버킷팅된다 (팩킹이 알아서 처리).

    재개: 이미 환율 청크가 있으면 마지막 청크 날짜부터 다시 시작한다
    (그 날짜는 다시 받아 병합 — 중복은 팩킹의 변동-정리로 걸러진다).
    """
    async with session_factory() as session:
        done_days = await store.get_fx_chunk_days(session)
    if done_days:
        resume_from = max(done_days)
        if resume_from > first_day:
            _log(f"fx — 기존 청크 발견, {resume_from} 부터 재개")
            first_day = resume_from

    today_kst = datetime.now(tz=KST).date()
    basis = first_day
    prev_rate: Decimal | None = None
    last_observed: tuple[int, Decimal] | None = None

    while basis <= today_kst:
        try:
            observations = await hana.fetch_day_rounds(
                basis, stride=stride, pace=pace
            )
        except Exception as exc:  # noqa: BLE001 — 하루 실패로 전체를 죽이지 않는다
            _log(f"fx {basis} — 실패, 건너뜀: {type(exc).__name__}: {exc}")
            await asyncio.sleep(10)
            basis += timedelta(days=1)
            continue
        if not observations:
            _log(f"fx {basis} — 고시 없음 (휴일)")
            basis += timedelta(days=1)
            continue

        # 회차 순서 = 시간 순서. 매매기준율이 직전과 달라진 고시만 남긴다.
        points: list[tuple[int, str]] = []
        for obs in observations:
            if prev_rate is None or obs.rate != prev_rate:
                points.append((obs.ts, str(obs.rate)))
                prev_rate = obs.rate
            last_observed = (obs.ts, obs.rate)

        async with session_factory() as session:
            await store.add_fx_points(session, points)
            packed = await service.pack_fx_days(
                session, now_ts=int(time.time())
            )
            await session.commit()
        _log(
            f"fx {basis} — 고시 {len(observations):,}건 → 변동 {len(points):,}건 "
            f"저장 (청크 {packed}개)"
        )
        basis += timedelta(days=1)

    if last_observed is not None:
        async with session_factory() as session:
            exchange, base = store.FX_CURSOR_KEY
            await _advance_cursor(
                session, exchange, base, last_observed[0], last_observed[1]
            )
            await session.commit()


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="가격 변동 이력 3개월 백필 (업비트 초봉 · 바이낸스 1s · 하나은행 환율)"
    )
    parser.add_argument(
        "--bases",
        default="BTC",
        help="백필할 코인 (쉼표 구분, 기본 BTC)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=92,
        help="며칠 전까지 거슬러 올라갈지 (기본 92 — 업비트 초봉 보관 한계에 맞춤)",
    )
    parser.add_argument(
        "--pace-upbit", type=float, default=0.2, help="업비트 요청 간격 초 (기본 0.2)"
    )
    parser.add_argument(
        "--pace-binance", type=float, default=0.1, help="바이낸스 요청 간격 초"
    )
    parser.add_argument("--skip-fx", action="store_true", help="환율 백필 생략")
    parser.add_argument(
        "--fx-stride",
        type=int,
        default=1,
        help="환율 고시 샘플링 간격 (1=전 회차, N=N회차마다 1건. 기본 1)",
    )
    parser.add_argument(
        "--fx-start",
        default=None,
        help="환율 백필 시작 기준일 YYYY-MM-DD (기본: --days 만큼 과거)",
    )
    parser.add_argument(
        "--fx-pace", type=float, default=0.35, help="하나은행 요청 간격 초"
    )
    args = parser.parse_args()

    bases = [b.strip().upper() for b in args.bases.split(",") if b.strip()]
    today_utc = datetime.now(tz=timezone.utc).date()
    #: 오늘(UTC)은 아직 완결되지 않았으므로 sync 몫으로 남기고 어제까지만 청크로.
    days = [
        today_utc - timedelta(days=n) for n in range(args.days, 0, -1)
    ]

    await init_db()  # 새 이력 테이블이 없으면 만든다
    session_factory = get_session_factory()

    started = time.perf_counter()
    try:
        for base in bases:
            _log(f"=== 업비트 {base} 백필 시작 ({days[0]} ~ {days[-1]}) ===")
            await backfill_coin_exchange(
                session_factory, "upbit", base, days, args.pace_upbit
            )
            _log(f"=== 바이낸스 {base} 백필 시작 ===")
            await backfill_coin_exchange(
                session_factory, "binance", base, days, args.pace_binance
            )

        if not args.skip_fx:
            fx_start = (
                date.fromisoformat(args.fx_start)
                if args.fx_start
                else today_utc - timedelta(days=args.days)
            )
            _log(f"=== 환율 백필 시작 ({fx_start} ~ 오늘, stride={args.fx_stride}) ===")
            await backfill_fx(session_factory, fx_start, args.fx_stride, args.fx_pace)
    finally:
        await shutdown_http_client()
        await dispose_engine()

    _log(f"백필 완료 — {time.perf_counter() - started:,.0f}초 소요")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n중단됨 — 다시 실행하면 이미 저장된 날은 건너뛰고 이어서 진행됩니다.")
        sys.exit(130)
