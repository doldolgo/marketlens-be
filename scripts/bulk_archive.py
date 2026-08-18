"""김프/역프 기록 대량 업데이트 — 아카이브 밖 시간대 구간을 캔들로 채운다.

``premium_archive`` 의 타임라인에서 **가장 처음과 가장 마지막 시각**을 읽고,
그 밖의 구간(목표 시작 ~ 첫 기록, 마지막 기록 ~ 지금)을 거래소 캔들로
한 번에 계산해 테이블 형식 그대로 저장한다.

    가격: 업비트 초봉 + 바이낸스 1초봉 (플랫폼이 지원하는 최소 간격)
    환율: 하나은행 고시 (--usdkrw-stride 간격 샘플링, 기본 30회차 ≈ 20분)
    계산: 종가 기준 김프/역프 — 셋 중 하나라도 변한 초마다 한 줄

사용 예 (EC2 의 marketlens-be 디렉토리에서):

    python -m scripts.bulk_archive --bases BTC
    python -m scripts.bulk_archive --bases BTC,ETH --days 92 --usdkrw-stride 15

동작 원칙
    - 하루(UTC) 단위로 수집→계산→저장→커밋한다. 중단돼도 재실행하면
      이미 저장된 시각은 건너뛰므로(ON CONFLICT DO NOTHING) 안전하다.
    - 업비트 초봉은 롤링 3개월만 보관되므로 --days 를 더 줘도 얻는 만큼만
      계산된다. 미룰수록 과거를 잃으니 배포 후 바로 한 번 돌려둘 것.
    - 대상 페어는 업비트 × 바이낸스다 (빗썸은 초봉 API 가 없어 대량 채우기
      불가 — 빗썸 페어 기록은 refresh 의 실시간 기록으로만 쌓인다).

주의: 업비트 레이트리밋(10 req/s)은 라이브 refresh 와 공유된다. 운영 중인
서버에서 돌릴 때는 기본 pace 를 유지할 것.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import datetime

from app.core.config import settings
from app.core.http import shutdown_http_client
from app.db.database import dispose_engine, get_session_factory, init_db
from app.db import repository
from app.history import service


def _log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="김프/역프 기록 대량 업데이트 (업비트 초봉 × 바이낸스 1초봉 × 하나은행 환율)"
    )
    parser.add_argument(
        "--bases",
        default=None,
        help="채울 코인 (쉼표 구분). 생략하면 설정 HISTORY_BASES (기본 BTC)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=92,
        help="며칠 전까지를 목표 구간으로 할지 (기본 92 — 업비트 초봉 보관 한계)",
    )
    parser.add_argument(
        "--usdkrw-stride",
        type=int,
        default=30,
        help="환율 고시 샘플링 간격 (N회차마다 1건, 기본 30 ≈ 20분 간격)",
    )
    parser.add_argument(
        "--pace-upbit", type=float, default=0.2, help="업비트 요청 간격 초"
    )
    parser.add_argument(
        "--pace-binance", type=float, default=0.1, help="바이낸스 요청 간격 초"
    )
    parser.add_argument(
        "--usdkrw-pace", type=float, default=0.35, help="하나은행 요청 간격 초"
    )
    args = parser.parse_args()

    bases = (
        [b.strip().upper() for b in args.bases.split(",") if b.strip()]
        if args.bases
        else [b.upper() for b in settings.history_bases]
    )
    now_ts = int(time.time())
    target_start = now_ts - args.days * 86_400

    await init_db()  # 새 테이블·뷰가 없으면 만든다
    session_factory = get_session_factory()
    started = time.perf_counter()

    try:
        # 1) 환율은 목표 전체 구간을 **한 번만** 수집해 모든 코인이 재사용한다.
        #    코인별 채울 구간은 전부 [target_start, now_ts) 의 부분집합이고,
        #    fill_premium_gap 이 날짜별로 슬라이스·씨앗을 알아서 고르므로
        #    상위 집합을 그대로 넘겨도 결과가 같다. 예전에는 코인마다 다시
        #    받아서 실측 코인당 ~50분씩 낭비됐다 (92일 × 고시 샘플링).
        usdkrw_events = await service.collect_usdkrw_events(
            target_start,
            now_ts,
            stride=args.usdkrw_stride,
            pace=args.usdkrw_pace,
            log=_log,
        )
        if not usdkrw_events:
            _log("환율을 하나도 수집하지 못해 중단합니다 (하나은행 조회 실패?)")
            return

        for base in bases:
            # 2) 아카이브의 첫/마지막 시각으로 채워야 할 구간을 계산한다.
            async with session_factory() as session:
                bounds = await repository.get_premium_bounds(
                    session, "upbit", "binance", base
                )
            ranges = service.missing_ranges(bounds, target_start, now_ts)
            if not ranges:
                _log(f"{base} — 목표 구간이 이미 전부 채워져 있습니다")
                continue
            _log(
                f"{base} — 기존 기록 {bounds}, 채울 구간 "
                + ", ".join(
                    f"[{datetime.fromtimestamp(s):%Y-%m-%d %H:%M} ~ "
                    f"{datetime.fromtimestamp(e):%Y-%m-%d %H:%M}]"
                    for s, e in ranges
                )
            )

            for range_start, range_end in ranges:
                # 3) 하루 단위로 캔들 수집 → 김프 계산 → 저장.
                #    기존 기록 "이전"(head) 구간은 최신 날부터 거꾸로 채운다 —
                #    중단돼도 남은 구간이 아카이브 경계 밖에 있어 재실행 시 이어진다.
                is_head = bounds is not None and range_end <= bounds[0]
                async with session_factory() as session:
                    saved = await service.fill_premium_gap(
                        session,
                        base,
                        range_start,
                        range_end,
                        usdkrw_events=usdkrw_events,
                        newest_first=is_head,
                        pace_upbit=args.pace_upbit,
                        pace_binance=args.pace_binance,
                        log=_log,
                    )
                _log(f"{base} — 구간 완료, 김프 기록 {saved:,}건 저장")
    finally:
        await shutdown_http_client()
        await dispose_engine()

    _log(f"대량 업데이트 완료 — {time.perf_counter() - started:,.0f}초 소요")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n중단됨 — 다시 실행하면 남은 구간부터 이어서 진행됩니다.")
        sys.exit(130)
