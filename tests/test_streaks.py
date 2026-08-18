"""김프/역프 구간 계산 테스트."""

from __future__ import annotations

import pytest
from conftest import seed_usdkrw_rate

from app.db import repository
from app.history.streaks import find_segments

#: 사용자가 제시한 예시 — 값 목록과 기준치별 기대 구간.
EXAMPLE = [0, 1, 3, 6, 29, 4, 31]


def points(values: list[float], *, step: int = 60) -> list[tuple[int, float]]:
    return [(i * step, float(v)) for i, v in enumerate(values)]


class TestFindSegments:
    def test_example_threshold_4_is_one_segment(self) -> None:
        """0 1 3 6 29 4 31 에 기준치 4 → 6 29 4 31 이 이어져 구간 1개."""
        stats = find_segments(points(EXAMPLE), 4)

        assert stats.count == 1
        seg = stats.segments[0]
        assert (seg.start_ts, seg.end_ts) == (180, 360)
        assert seg.samples == 4
        assert seg.max_percent == 31.0

    def test_example_threshold_5_splits_into_two(self) -> None:
        """기준치 5 → 4 가 탈락하며 이어짐이 끊겨 구간 2개."""
        stats = find_segments(points(EXAMPLE), 5)

        assert stats.count == 2
        assert [(s.start_ts, s.end_ts) for s in stats.segments] == [(180, 240), (360, 360)]
        assert [s.max_percent for s in stats.segments] == [29.0, 31.0]

    def test_threshold_is_inclusive(self) -> None:
        """'이상' 이다 — 기준치와 똑같은 값은 남는다."""
        assert find_segments(points([4]), 4).count == 1
        assert find_segments(points([3.999]), 4).count == 0

    def test_single_sample_segment_has_zero_duration(self) -> None:
        stats = find_segments(points(EXAMPLE), 5)

        lone = stats.segments[1]
        assert lone.samples == 1
        assert lone.duration_seconds == 0

    def test_gap_breaks_a_segment(self) -> None:
        """수집이 끊긴 구멍을 이어 붙이면 없던 지속 시간이 생긴다."""
        pts = [(0, 9.0), (60, 9.0), (100_000, 9.0)]

        joined = find_segments(pts, 1, max_gap_seconds=200_000)
        assert joined.count == 1
        assert joined.segments[0].duration_seconds == 100_000

        split = find_segments(pts, 1, max_gap_seconds=600)
        assert split.count == 2
        assert [s.duration_seconds for s in split.segments] == [60, 0]

    def test_negative_values_never_qualify_for_positive_threshold(self) -> None:
        """절댓값을 쓰지 않는다 — 큰 음수는 그 방향의 수익이 아니다."""
        assert find_segments(points([-30, -29, -31]), 1).count == 0

    def test_avg_is_weighted_by_samples(self) -> None:
        """구간 평균의 평균이 아니라 기록 전체의 평균이어야 한다."""
        # 구간 A: 10 이 3건, 구간 B: 30 이 1건 (사이에 0 으로 끊김)
        stats = find_segments(points([10, 10, 10, 0, 30]), 5)

        assert stats.count == 2
        # 단순 평균이면 (10 + 30) / 2 = 20. 가중 평균은 (10*3 + 30) / 4 = 15.
        assert stats.avg_percent == pytest.approx(15.0)

    def test_empty_input(self) -> None:
        stats = find_segments([], 1)

        assert stats.count == 0
        assert stats.max_duration_seconds == 0
        assert stats.max_percent == 0.0
        assert stats.avg_percent == 0.0


class TestStreaksEndpoint:
    async def test_separates_kimp_and_reverse(self, client, db) -> None:
        """fwd 와 rev 는 각각 따로 구간을 만든다."""
        await seed_usdkrw_rate(db)
        await repository.add_premium_rows(
            db,
            [
                {"dom": "upbit", "fx": "binance", "base": "BTC", "ts": t,
                 "fwd": fwd, "rev": rev}
                for t, fwd, rev in [
                    (1000, 2.0, -2.5),
                    (1060, 3.0, -3.5),
                    (1120, -1.0, 0.5),
                    (1180, -4.0, 3.0),
                ]
            ],
        )
        await db.commit()

        res = await client.get("/history/streaks?base=BTC&threshold=1")
        assert res.status_code == 200
        body = res.json()

        assert body["kimp"]["count"] == 1
        assert body["kimp"]["segments"][0]["duration_seconds"] == 60
        assert body["kimp"]["max_percent"] == pytest.approx(3.0)

        assert body["reverse"]["count"] == 1
        assert body["reverse"]["max_percent"] == pytest.approx(3.0)
        assert body["last_updated_ts"] == 1180

    async def test_missing_coin_is_404(self, client) -> None:
        res = await client.get("/history/streaks?base=NOPE")
        assert res.status_code == 404

    async def test_negative_threshold_is_422(self, client) -> None:
        res = await client.get("/history/streaks?base=BTC&threshold=-1")
        assert res.status_code == 422

    async def test_overall_ignores_threshold(self, client, db) -> None:
        """전체 통계는 기준치를 넘지 못한 기록·음수까지 모두 포함한다."""
        await seed_usdkrw_rate(db)
        await repository.add_premium_rows(
            db,
            [
                {"dom": "upbit", "fx": "binance", "base": "ETH", "ts": t,
                 "fwd": fwd, "rev": rev}
                for t, fwd, rev in [
                    (1000, 0.1, -0.5),
                    (1060, 0.3, -0.7),
                    (1120, -0.2, 0.4),
                ]
            ],
        )
        await db.commit()

        # 기준치 99 — 구간은 하나도 안 잡히지만 전체 통계는 그대로 나온다.
        res = await client.get("/history/streaks?base=ETH&threshold=99")
        body = res.json()

        assert body["kimp"]["count"] == 0
        assert body["reverse"]["count"] == 0

        overall = body["overall"]
        assert overall["max_kimp_percent"] == pytest.approx(0.3)
        assert overall["avg_kimp_percent"] == pytest.approx((0.1 + 0.3 - 0.2) / 3)
        assert overall["max_reverse_percent"] == pytest.approx(0.4)
        assert overall["avg_reverse_percent"] == pytest.approx((-0.5 - 0.7 + 0.4) / 3)
        # 지속 시간은 구간 개념이라 기준치를 탄다 — 구간이 없으니 0.
        assert overall["max_duration_seconds"] == 0
        assert overall["segment_count"] == 0

    async def test_bulk_returns_all_coins_in_one_call(self, client, db) -> None:
        """벌크는 기록이 있는 코인 전부를 심볼 순으로 한 번에 준다."""
        await seed_usdkrw_rate(db)
        rows = []
        for base, rev_level in [("BTC", 2.0), ("ETH", 0.2), ("XRP", 1.5)]:
            rows += [
                {"dom": "upbit", "fx": "binance", "base": base, "ts": t,
                 "fwd": -rev_level, "rev": rev_level}
                for t in (1000, 1060, 1120)
            ]
        await repository.add_premium_rows(db, rows)
        await db.commit()

        res = await client.get("/history/streaks/bulk?threshold=1&bucket=0")
        assert res.status_code == 200
        body = res.json()

        assert body["coin_count"] == 3
        assert [c["base"] for c in body["coins"]] == ["BTC", "ETH", "XRP"]

        by_base = {c["base"]: c for c in body["coins"]}
        # BTC·XRP 는 rev 1% 이상이 3건 연속 → 구간 1개 (120초).
        for sym in ("BTC", "XRP"):
            assert by_base[sym]["reverse"]["count"] == 1
            assert by_base[sym]["reverse"]["max_duration_seconds"] == 120
            assert by_base[sym]["kimp"]["count"] == 0
        # ETH 는 기준치 미달 — 구간은 없지만 전체 통계는 있다.
        assert by_base["ETH"]["reverse"]["count"] == 0
        assert by_base["ETH"]["overall"]["max_reverse_percent"] == pytest.approx(0.2)
        assert by_base["ETH"]["scanned"] == 3
        assert by_base["ETH"]["last_ts"] == 1120

    async def test_bulk_matches_single_endpoint(self, client, db) -> None:
        """같은 데이터라면 벌크(bucket=0)와 단건의 구간 통계가 일치해야 한다."""
        await seed_usdkrw_rate(db)
        values = [0.3, 0.8, 0.9, 0.2, 1.4, 0.6, 0.7]
        await repository.add_premium_rows(
            db,
            [
                {"dom": "upbit", "fx": "binance", "base": "BTC",
                 "ts": 1000 + i * 60, "fwd": -v, "rev": v}
                for i, v in enumerate(values)
            ],
        )
        await db.commit()

        single = (await client.get("/history/streaks?base=BTC&threshold=0.5")).json()
        bulk = (await client.get("/history/streaks/bulk?threshold=0.5&bucket=0")).json()

        coin = bulk["coins"][0]
        assert coin["reverse"] == single["reverse"]
        assert coin["kimp"] == single["kimp"]
        assert coin["overall"] == single["overall"]
        assert coin["scanned"] == single["scanned"]

    async def test_bulk_bucket_keeps_last_record_per_bucket(self, client, db) -> None:
        """bucket=60 이면 60초 버킷마다 마지막 기록 하나만 남는다."""
        await seed_usdkrw_rate(db)
        # 버킷 [960,1020): 3건 (마지막 1019), 버킷 [1020,1080): 1건.
        await repository.add_premium_rows(
            db,
            [
                {"dom": "upbit", "fx": "binance", "base": "BTC", "ts": t,
                 "fwd": fwd, "rev": -fwd}
                for t, fwd in [(961, 5.0), (990, 6.0), (1019, 7.0), (1030, 8.0)]
            ],
        )
        await db.commit()

        body = (await client.get("/history/streaks/bulk?threshold=1&bucket=60")).json()

        coin = body["coins"][0]
        assert coin["scanned"] == 2
        seg = coin["kimp"]["segments"][0]
        # 남는 기록은 각 버킷의 마지막 — 1019(값 7)와 1030(값 8).
        assert (seg["start_ts"], seg["end_ts"]) == (1019, 1030)
        assert seg["max_percent"] == pytest.approx(8.0)
        assert coin["kimp"]["count"] == 1

    async def test_bulk_bases_filter_and_empty_result(self, client, db) -> None:
        """bases 필터는 지정 코인만 남기고, 기록이 없으면 404 대신 빈 목록이다."""
        await seed_usdkrw_rate(db)
        await repository.add_premium_rows(
            db,
            [
                {"dom": "upbit", "fx": "binance", "base": base, "ts": 1000,
                 "fwd": 1.0, "rev": -1.0}
                for base in ("BTC", "ETH")
            ],
        )
        await db.commit()

        body = (await client.get("/history/streaks/bulk?bases=eth")).json()
        assert [c["base"] for c in body["coins"]] == ["ETH"]

        empty = await client.get("/history/streaks/bulk?start=999999999")
        assert empty.status_code == 200
        assert empty.json()["coins"] == []
        assert empty.json()["coin_count"] == 0

    async def test_overall_duration_merges_both_directions(self, client, db) -> None:
        """전체 지속은 김프 구간과 역프 구간을 합쳐서 낸다."""
        await seed_usdkrw_rate(db)
        await repository.add_premium_rows(
            db,
            [
                {"dom": "upbit", "fx": "binance", "base": "XRP", "ts": t,
                 "fwd": fwd, "rev": rev}
                for t, fwd, rev in [
                    (1000, 5.0, -5.0),   # 김프 구간 시작
                    (1060, 5.0, -5.0),   # 김프 구간 끝 (60초)
                    (1120, -5.0, 5.0),   # 역프 구간 (1건, 0초)
                ]
            ],
        )
        await db.commit()

        body = (await client.get("/history/streaks?base=XRP&threshold=1")).json()

        assert body["kimp"]["count"] == 1
        assert body["reverse"]["count"] == 1
        overall = body["overall"]
        assert overall["segment_count"] == 2
        assert overall["max_duration_seconds"] == 60
        assert overall["avg_duration_seconds"] == pytest.approx(30.0)
