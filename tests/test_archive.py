"""김프/역프 아카이브 · 플랫폼 상태 테스트 (네트워크 불필요).

핵심 검증 대상:
    1. 김프/역프 계산식 — 호가 기준(실시간)과 종가 기준(대량)의 정확성
    2. missing_ranges — 아카이브 첫/마지막 시각 밖 구간 계산
    3. merge_premium_timeline — 세 변동 시계열의 forward-fill 병합
    4. 아카이브 저장 — 중복 무시, 범위 조회, 경계 조회
    5. 플랫폼 상태 — 카운터 누적(+1), 실패율 재료
    6. API — 절대 타임라인 → 상대 시간 로그(dt) 변환
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.db import repository
from app.history import service

# 기준 시각: 2026-08-12 00:00 UTC (수요일)
DAY = date(2026, 8, 12)
DAY_TS = int(datetime(2026, 8, 12, tzinfo=timezone.utc).timestamp())


class TestPremiumFormulas:
    def test_quotes_formula_matches_spreads(self) -> None:
        """호가 기준 김프/역프 — /spreads 와 같은 공식."""
        # 국내 bid 100,100,000 / 해외 ask 71,000×1400 = 99,400,000 → 김프 약 +0.7%
        result = service.premium_from_quotes(
            dom_bid=100_100_000, dom_ask=100_200_000,
            fx_bid=70_900, fx_ask=71_000, rate=1400.0,
        )
        assert result is not None
        fwd, rev = result
        assert fwd == pytest.approx((100_100_000 / (71_000 * 1400) - 1) * 100)
        assert rev == pytest.approx((70_900 * 1400 / 100_200_000 - 1) * 100)
        assert fwd > 0 and rev < 0  # 국내가 비싼 시나리오

    def test_quotes_formula_rejects_invalid(self) -> None:
        assert service.premium_from_quotes(0, 1, 1, 1, 1) is None
        assert service.premium_from_quotes(1, 1, 1, 1, 0) is None

    def test_closes_formula_is_symmetric(self) -> None:
        """종가 기준은 양방향이 같은 가격을 쓰므로 역수 관계다."""
        result = service.premium_from_closes(100_000_000, 71_000, 1400.0)
        assert result is not None
        fwd, rev = result
        ratio = 100_000_000 / (71_000 * 1400)
        assert fwd == pytest.approx((ratio - 1) * 100)
        assert rev == pytest.approx((1 / ratio - 1) * 100)


class TestMissingRanges:
    def test_empty_archive_fills_whole_target(self) -> None:
        assert service.missing_ranges(None, 100, 200) == [(100, 200)]

    def test_gaps_before_and_after_existing(self) -> None:
        """기존 기록의 첫/마지막 시각 밖 두 구간만 채운다."""
        assert service.missing_ranges((150, 170), 100, 200) == [
            (100, 150),
            (171, 200),
        ]

    def test_fully_covered_returns_nothing(self) -> None:
        assert service.missing_ranges((100, 199), 100, 200) == []

    def test_only_tail_missing(self) -> None:
        assert service.missing_ranges((100, 150), 100, 200) == [(151, 200)]


class TestMergeTimeline:
    def test_forward_fill_and_change_only(self) -> None:
        """셋 중 하나라도 변한 초마다 한 줄 — 나머지 값은 직전 값 유지."""
        rows, seeds = service.merge_premium_timeline(
            upbit_events=[(DAY_TS, Decimal("100000000")), (DAY_TS + 10, Decimal("100100000"))],
            binance_events=[(DAY_TS, Decimal("71000"))],
            fx_events=[(DAY_TS, Decimal("1400"))],
        )
        assert [r["ts"] for r in rows] == [DAY_TS, DAY_TS + 10]
        # 두 번째 줄: 업비트만 변했고 바이낸스·환율은 forward-fill
        expected = (100_100_000 / (71_000 * 1400) - 1) * 100
        assert rows[1]["fwd"] == pytest.approx(expected)
        assert seeds == (Decimal("100100000"), Decimal("71000"), Decimal("1400"))

    def test_skips_until_all_three_available(self) -> None:
        """세 값이 다 갖춰지기 전의 초는 계산하지 않는다."""
        rows, _ = service.merge_premium_timeline(
            upbit_events=[(DAY_TS, Decimal("100000000"))],
            binance_events=[(DAY_TS + 5, Decimal("71000"))],
            fx_events=[(DAY_TS + 9, Decimal("1400"))],
        )
        assert [r["ts"] for r in rows] == [DAY_TS + 9]  # 환율까지 온 뒤 첫 줄

    def test_seeds_carry_over_between_days(self) -> None:
        """전날 씨앗이 있으면 첫 초부터 계산된다."""
        rows, _ = service.merge_premium_timeline(
            upbit_events=[(DAY_TS + 3, Decimal("100000000"))],
            binance_events=[],
            fx_events=[],
            seeds=(Decimal("99000000"), Decimal("71000"), Decimal("1400")),
        )
        assert [r["ts"] for r in rows] == [DAY_TS + 3]

    def test_unchanged_premium_not_recorded(self) -> None:
        """입력이 갱신돼도 김프 값이 그대로면 줄을 만들지 않는다."""
        rows, _ = service.merge_premium_timeline(
            upbit_events=[(DAY_TS, Decimal("100000000")), (DAY_TS + 5, Decimal("100000000"))],
            binance_events=[(DAY_TS, Decimal("71000"))],
            fx_events=[(DAY_TS, Decimal("1400"))],
        )
        assert len(rows) == 1  # 같은 값 재관측은 기록 없음


class TestArchiveStore:
    async def test_insert_dedup_and_range(self, db) -> None:
        rows = [
            {"dom": "upbit", "fx": "binance", "base": "BTC", "ts": DAY_TS, "fwd": 0.5, "rev": -0.6},
            {"dom": "upbit", "fx": "binance", "base": "BTC", "ts": DAY_TS + 60, "fwd": 0.7, "rev": -0.8},
        ]
        await repository.add_premium_rows(db, rows)
        # 같은 (dom, fx, base, ts) 재삽입은 무시된다 — refresh 와 대량 채우기가 겹쳐도 안전
        await repository.add_premium_rows(
            db,
            [{"dom": "upbit", "fx": "binance", "base": "BTC", "ts": DAY_TS, "fwd": 9.9, "rev": 9.9}],
        )
        await db.commit()

        got = await repository.get_premium_range(
            db, "upbit", "binance", "BTC", DAY_TS, DAY_TS + 3600
        )
        assert [(r.ts, r.fwd) for r in got] == [(DAY_TS, 0.5), (DAY_TS + 60, 0.7)]

    async def test_bounds(self, db) -> None:
        assert await repository.get_premium_bounds(db, "upbit", "binance", "BTC") is None
        await repository.add_premium_rows(
            db,
            [
                {"dom": "upbit", "fx": "binance", "base": "BTC", "ts": DAY_TS, "fwd": 0.1, "rev": -0.1},
                {"dom": "upbit", "fx": "binance", "base": "BTC", "ts": DAY_TS + 100, "fwd": 0.2, "rev": -0.2},
            ],
        )
        await db.commit()
        assert await repository.get_premium_bounds(db, "upbit", "binance", "BTC") == (
            DAY_TS,
            DAY_TS + 100,
        )


class TestPlatformStatus:
    async def test_counters_accumulate(self, db) -> None:
        """update_count 는 매번 +1, dw_fail_count 는 실패 관측 회차만 +1."""
        await repository.bump_platform_status(
            db, exchange="upbit", received_ts=DAY_TS,
            spot_market_count=180, futures_market_count=0, dw_failed=False,
        )
        await repository.bump_platform_status(
            db, exchange="upbit", received_ts=DAY_TS + 60,
            spot_market_count=181, futures_market_count=0, dw_failed=True,
        )
        await repository.bump_platform_status(
            db, exchange="upbit", received_ts=DAY_TS + 120,
            spot_market_count=181, futures_market_count=0, dw_failed=False,
        )
        await db.commit()

        rows = await repository.get_platform_statuses(db)
        assert len(rows) == 1
        row = rows[0]
        assert row.last_received_ts == DAY_TS + 120
        assert row.update_count == 3
        assert row.dw_fail_count == 1  # 실패율 = 1/3
        assert row.spot_market_count == 181  # 최신 관측값으로 덮어씀

    async def test_none_futures_keeps_previous(self, db) -> None:
        """선물 수를 못 센 회차(None)는 이전 값을 유지한다 — 0 으로 덮지 않는다."""
        await repository.bump_platform_status(
            db, exchange="binance", received_ts=DAY_TS,
            spot_market_count=3700, futures_market_count=500, dw_failed=False,
        )
        await repository.bump_platform_status(
            db, exchange="binance", received_ts=DAY_TS + 60,
            spot_market_count=3700, futures_market_count=None, dw_failed=False,
        )
        await db.commit()

        row = (await repository.get_platform_statuses(db))[0]
        assert row.futures_market_count == 500
        assert row.update_count == 2


class TestHistoryApi:
    async def _seed(self, db) -> None:
        await repository.add_premium_rows(
            db,
            [
                {"dom": "upbit", "fx": "binance", "base": "BTC", "ts": DAY_TS + 0, "fwd": 0.50, "rev": -0.55},
                {"dom": "upbit", "fx": "binance", "base": "BTC", "ts": DAY_TS + 5, "fwd": 0.62, "rev": -0.67},
                {"dom": "upbit", "fx": "binance", "base": "BTC", "ts": DAY_TS + 12, "fwd": 0.44, "rev": -0.49},
            ],
        )
        await db.commit()

    async def test_premium_log_returns_deltas(self, client, db) -> None:
        await self._seed(db)
        r = await client.get(
            "/history/premium",
            params={"base": "BTC", "unit": "week", "date": DAY.isoformat()},
        )
        assert r.status_code == 200
        d = r.json()

        assert d["dom"] == "upbit" and d["fx"] == "binance"
        assert d["first_ts"] == DAY_TS  # 절대 시각은 first_ts 하나만
        assert d["count"] == 3
        # dt = 직전 기록에서 몇 초 뒤인가
        assert [e["dt"] for e in d["events"]] == [0, 5, 7]
        assert [e["fwd"] for e in d["events"]] == [0.50, 0.62, 0.44]
        assert d["first_ts"] + sum(e["dt"] for e in d["events"]) == DAY_TS + 12
        assert d["summary"]["max_fwd"] == 0.62

    async def test_pagination_keeps_delta_continuity(self, client, db) -> None:
        await self._seed(db)
        params = {"base": "BTC", "unit": "week", "date": DAY.isoformat(), "limit": 2}
        page1 = (await client.get("/history/premium", params=params)).json()
        page2 = (
            await client.get("/history/premium", params={**params, "offset": 2})
        ).json()
        assert page1["has_more"] is True and page2["has_more"] is False
        assert [e["dt"] for e in page1["events"]] == [0, 5]
        assert [e["dt"] for e in page2["events"]] == [7]  # 직전(offset-1) 대비

    async def test_month_unit(self, client, db) -> None:
        await self._seed(db)
        d = (
            await client.get(
                "/history/premium",
                params={"base": "BTC", "unit": "month", "date": "2026-08-25"},
            )
        ).json()
        assert d["count"] == 3
        assert d["start"].startswith("2026-08-01")

    async def test_empty_period_is_404(self, client, db) -> None:
        await self._seed(db)
        r = await client.get(
            "/history/premium",
            params={"base": "BTC", "unit": "week", "date": "2025-01-01"},
        )
        assert r.status_code == 404

    async def test_status_endpoint(self, client, db) -> None:
        await repository.bump_platform_status(
            db, exchange="upbit", received_ts=DAY_TS,
            spot_market_count=180, futures_market_count=0, dw_failed=True,
        )
        await repository.bump_platform_status(
            db, exchange="upbit", received_ts=DAY_TS + 60,
            spot_market_count=180, futures_market_count=0, dw_failed=False,
        )
        await db.commit()

        d = (await client.get("/history/status")).json()
        entry = d["platforms"][0]
        assert entry["exchange"] == "upbit"
        assert entry["update_count"] == 2
        assert entry["dw_fail_count"] == 1
        assert entry["dw_fail_rate"] == pytest.approx(0.5)

    async def test_status_empty_is_404(self, client) -> None:
        assert (await client.get("/history/status")).status_code == 404
