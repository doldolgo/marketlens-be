"""가격 변동 이력 서브시스템 테스트 — 코덱 · 팩킹 · 조회 API (네트워크 불필요).

핵심 검증 대상:
    1. 코덱 round-trip — 압축 → 복원이 비트 단위로 원본과 같다 (무손실)
    2. 변동 축약(keep_changes) — 같은 가격 연속 구간 제거의 정확성
    3. 팩킹 — 완결된 날이 스테이징에서 청크로 옮겨지고 조회가 병합된다
    4. API — 절대 타임라인(DB) → 상대 시간 로그(dt/diff) 변환의 정확성
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.history import codec, service, store

# 기준 시각: 2026-08-12 (수요일) 00:00 UTC — 완결된 하루를 다루기 좋다.
DAY = date(2026, 8, 12)
DAY_TS = int(datetime(2026, 8, 12, tzinfo=timezone.utc).timestamp())
#: "지금" 은 이틀 뒤 정오 — DAY 와 DAY+1 이 모두 완결된 과거가 되게 한다.
NOW_TS = DAY_TS + 2 * 86_400 + 43_200


class TestCodec:
    """무손실 압축 코덱 — 저장의 심장."""

    def test_round_trip_is_exact(self) -> None:
        points = [
            (DAY_TS, 90_191_000),
            (DAY_TS + 1, 90_191_000),  # 같은 가격 (축약은 코덱 밖의 일)
            (DAY_TS + 4, 90_192_500),
            (DAY_TS + 1000, 90_150_000),
            (DAY_TS + 86_399, 91_000_000),
        ]
        assert codec.decode_points(codec.encode_points(points)) == points

    def test_verified_encode_asserts_round_trip(self) -> None:
        points = [(DAY_TS + i * 3, 1000 + (i % 7) - 3) for i in range(5_000)]
        blob = codec.encode_points_verified(points)
        assert codec.decode_points(blob) == points
        # 압축이 실제로 이득인지 — 포인트당 16바이트(원시)보다 훨씬 작아야 한다
        assert len(blob) < len(points) * 4

    def test_negative_and_zero_deltas_survive(self) -> None:
        """가격 하락(음수 델타)과 동일 가격이 정확히 복원된다 (zigzag 검증)."""
        points = [(DAY_TS, 100), (DAY_TS + 1, 50), (DAY_TS + 2, 50), (DAY_TS + 3, 150)]
        assert codec.decode_points(codec.encode_points(points)) == points

    def test_unsorted_timestamps_rejected(self) -> None:
        with pytest.raises(ValueError):
            codec.encode_points([(DAY_TS + 1, 1), (DAY_TS, 2)])

    def test_empty_rejected(self) -> None:
        with pytest.raises(ValueError):
            codec.encode_points([])

    def test_decimal_scaling_is_lossless(self) -> None:
        """십진 문자열 → 스케일 정수 → 십진 복원이 값 그대로다."""
        values = [Decimal("1518.4"), Decimal("1517.95"), Decimal("1520")]
        scale = codec.decimal_scale(values)
        assert scale == 2
        for v in values:
            assert codec.from_scaled(codec.to_scaled(v, scale), scale) == v

    def test_scale_mismatch_raises(self) -> None:
        """스케일이 부족하면 조용히 반올림하지 않고 예외를 던진다."""
        with pytest.raises(ValueError):
            codec.to_scaled(Decimal("1518.45"), 1)


class TestKeepChanges:
    def test_drops_unchanged_prices(self) -> None:
        points = [
            (1, Decimal("100")),
            (2, Decimal("100")),  # 변동 없음 — 버려진다
            (3, Decimal("101")),
            (4, Decimal("101")),  # 버려진다
            (5, Decimal("100")),
        ]
        assert service.keep_changes(points, None) == [
            (1, Decimal("100")),
            (3, Decimal("101")),
            (5, Decimal("100")),
        ]

    def test_seed_suppresses_leading_duplicates(self) -> None:
        """직전 구간 마지막 가격(seed)과 같은 첫 포인트들도 변동이 아니다."""
        points = [(1, Decimal("100")), (2, Decimal("101"))]
        assert service.keep_changes(points, Decimal("100")) == [(2, Decimal("101"))]

    def test_without_seed_first_point_kept(self) -> None:
        points = [(1, Decimal("100"))]
        assert service.keep_changes(points, None) == points


class TestPeriodRange:
    def test_week_is_iso_monday_to_monday(self) -> None:
        # 2026-08-12 는 수요일 → 그 주는 08-10(월) ~ 08-17(월)
        start, end = service.period_range("week", date(2026, 8, 12))
        assert datetime.fromtimestamp(start, tz=timezone.utc).date() == date(2026, 8, 10)
        assert end - start == 7 * 86_400

    def test_month_covers_calendar_month(self) -> None:
        start, end = service.period_range("month", date(2026, 8, 12))
        assert datetime.fromtimestamp(start, tz=timezone.utc).date() == date(2026, 8, 1)
        assert datetime.fromtimestamp(end, tz=timezone.utc).date() == date(2026, 9, 1)

    def test_invalid_unit_raises(self) -> None:
        with pytest.raises(ValueError):
            service.period_range("day", date(2026, 8, 12))


class TestPackAndLoad:
    """스테이징 → 청크 팩킹과 병합 조회."""

    async def test_pack_moves_completed_days_to_chunks(self, db) -> None:
        points = [
            (DAY_TS + 0, "90191000"),
            (DAY_TS + 4, "90192500"),
            (DAY_TS + 90_000, "90300000"),  # 다음날 (DAY+1) 이벤트
        ]
        await store.add_price_points(db, "upbit", "BTC", points)

        packed = await service.pack_price_days(db, "upbit", "BTC", now_ts=NOW_TS)
        await db.commit()
        assert packed == 2  # DAY 와 DAY+1 (둘 다 NOW 기준 과거)

        # 스테이징은 비워지고 청크만 남는다.
        assert await store.get_price_points(db, "upbit", "BTC", 0, NOW_TS) == []
        chunks = await store.get_price_chunks(
            db, "upbit", "BTC", DAY, DAY + timedelta(days=1)
        )
        assert [c.n_points for c in chunks] == [2, 1]
        assert chunks[0].first_price == 90_191_000

        # 병합 조회는 청크를 풀어 원본 절대 타임라인을 돌려준다.
        events = await service.load_price_events(
            db, "upbit", "BTC", DAY_TS, DAY_TS + 2 * 86_400
        )
        assert [(ts, str(p)) for ts, p in events] == points

    async def test_pack_merges_into_existing_chunk(self, db) -> None:
        """백필과 주기 수집이 같은 날을 겹쳐 써도 데이터가 유실되지 않는다."""
        await store.add_price_points(db, "upbit", "BTC", [(DAY_TS, "100")])
        await service.pack_price_days(db, "upbit", "BTC", now_ts=NOW_TS)
        await store.add_price_points(db, "upbit", "BTC", [(DAY_TS + 10, "101")])
        await service.pack_price_days(db, "upbit", "BTC", now_ts=NOW_TS)
        await db.commit()

        events = await service.load_price_events(
            db, "upbit", "BTC", DAY_TS, DAY_TS + 86_400
        )
        assert [(ts, str(p)) for ts, p in events] == [
            (DAY_TS, "100"),
            (DAY_TS + 10, "101"),
        ]

    async def test_today_stays_in_staging_and_still_loads(self, db) -> None:
        """오늘(미완결) 데이터는 청크가 안 되지만 조회에는 나온다."""
        today_ts = NOW_TS - 3_600
        await store.add_price_points(db, "upbit", "BTC", [(today_ts, "42")])
        packed = await service.pack_price_days(db, "upbit", "BTC", now_ts=NOW_TS)
        assert packed == 0

        events = await service.load_price_events(
            db, "upbit", "BTC", today_ts - 10, NOW_TS
        )
        assert [(ts, str(p)) for ts, p in events] == [(today_ts, "42")]

    async def test_fx_pack_and_load(self, db) -> None:
        await store.add_fx_points(db, [(DAY_TS, "1518.4"), (DAY_TS + 44, "1518.95")])
        packed = await service.pack_fx_days(db, now_ts=NOW_TS)
        await db.commit()
        assert packed == 1

        events = await service.load_fx_events(db, DAY_TS, DAY_TS + 86_400)
        # Decimal 값으로 비교한다 — 1518.4 와 1518.40 은 같은 값이다
        # (스케일 정수 복원이 뒤 0 표기까지 보존하지는 않는다).
        assert [(ts, p) for ts, p in events] == [
            (DAY_TS, Decimal("1518.4")),
            (DAY_TS + 44, Decimal("1518.95")),
        ]


class TestHistoryApi:
    """상대 시간 로그 변환 — DB 절대 타임라인 → dt/diff."""

    async def _seed_coin(self, db) -> None:
        await store.add_price_points(
            db,
            "upbit",
            "BTC",
            [
                (DAY_TS + 0, "100000000"),
                (DAY_TS + 5, "100010000"),
                (DAY_TS + 12, "99995000"),
            ],
        )
        await db.commit()

    async def test_coin_log_returns_deltas(self, client, db) -> None:
        await self._seed_coin(db)
        r = await client.get(
            "/history/coin",
            params={
                "exchange": "upbit",
                "base": "BTC",
                "unit": "week",
                "date": DAY.isoformat(),
            },
        )
        assert r.status_code == 200
        d = r.json()

        assert d["exchange"] == "upbit" and d["quote"] == "KRW"
        assert d["first_ts"] == DAY_TS  # 절대 시각은 first_ts 하나만
        assert d["count"] == 3
        # dt = 직전 변동에서 몇 초 뒤, diff = 얼마만큼 변했는가
        assert [e["dt"] for e in d["events"]] == [0, 5, 7]
        assert [e["diff"] for e in d["events"]] == [0.0, 10000.0, -15000.0]
        assert [e["price"] for e in d["events"]] == [1e8, 100010000.0, 99995000.0]
        # first_ts + dt 누적 = 절대 타임라인 복원
        assert d["first_ts"] + sum(e["dt"] for e in d["events"]) == DAY_TS + 12
        assert d["summary"]["change_percent"] == pytest.approx(-0.005)

    async def test_pagination_keeps_delta_continuity(self, client, db) -> None:
        """페이지를 나눠 받아도 dt 는 전체 열 기준이라 이어 붙이면 완전하다."""
        await self._seed_coin(db)
        params = {
            "exchange": "upbit",
            "base": "BTC",
            "unit": "week",
            "date": DAY.isoformat(),
            "limit": 2,
        }
        page1 = (await client.get("/history/coin", params=params)).json()
        page2 = (
            await client.get("/history/coin", params={**params, "offset": 2})
        ).json()

        assert page1["has_more"] is True and page2["has_more"] is False
        assert [e["dt"] for e in page1["events"]] == [0, 5]
        assert [e["dt"] for e in page2["events"]] == [7]  # 직전(offset-1) 대비

    async def test_month_unit_selects_calendar_month(self, client, db) -> None:
        await self._seed_coin(db)
        d = (
            await client.get(
                "/history/coin",
                params={
                    "exchange": "upbit",
                    "base": "BTC",
                    "unit": "month",
                    "date": "2026-08-25",  # 같은 8월이면 어느 날짜든 같은 구간
                },
            )
        ).json()
        assert d["count"] == 3
        assert d["start"].startswith("2026-08-01")

    async def test_empty_period_is_404(self, client, db) -> None:
        await self._seed_coin(db)
        r = await client.get(
            "/history/coin",
            params={
                "exchange": "upbit",
                "base": "BTC",
                "unit": "week",
                "date": "2025-01-01",
            },
        )
        assert r.status_code == 404

    async def test_fx_log_is_separate_endpoint(self, client, db) -> None:
        await store.add_fx_points(db, [(DAY_TS, "1518.4"), (DAY_TS + 44, "1519")])
        await db.commit()

        d = (
            await client.get(
                "/history/fx", params={"unit": "week", "date": DAY.isoformat()}
            )
        ).json()
        assert d["count"] == 2
        assert [e["dt"] for e in d["events"]] == [0, 44]
        assert d["events"][1]["price"] == 1519.0
        assert "exchange" not in d  # 환율에는 거래소 개념이 없다


class TestSyncCursor:
    """증분 수집 — 커서와 변동 축약의 결합 (거래소 호출은 모킹)."""

    async def test_sync_upbit_stores_changes_and_advances_cursor(
        self, db, monkeypatch
    ) -> None:
        observed = [
            (DAY_TS + 0, Decimal("100")),
            (DAY_TS + 1, Decimal("100")),  # 변동 없음
            (DAY_TS + 2, Decimal("101")),
        ]

        async def fake_fetch(base, start_ts, end_ts, **kwargs):
            return [p for p in observed if start_ts <= p[0] < end_ts]

        monkeypatch.setattr(
            "app.history.upbit.fetch_seconds_range", fake_fetch
        )

        saved = await service.sync_upbit(db, "BTC", now_ts=DAY_TS + 10)
        await db.commit()
        assert saved == 2  # 100(첫 관측), 101(변동)

        cursor = await store.get_cursor(db, "upbit", "BTC")
        # 커서는 마지막 "관측"(변동 여부 무관)까지 전진한다.
        assert cursor.last_ts == DAY_TS + 2
        assert cursor.last_price == "101"

        # 두 번째 sync — 새 데이터가 없으면 아무것도 저장하지 않는다.
        saved = await service.sync_upbit(db, "BTC", now_ts=DAY_TS + 10)
        assert saved == 0

    async def test_sync_excludes_in_progress_second(self, db, monkeypatch) -> None:
        """진행 중인 현재 초의 캔들은 받지 않는다 — 확정 전 가격이기 때문."""
        captured: dict = {}

        async def fake_fetch(base, start_ts, end_ts, **kwargs):
            captured["end_ts"] = end_ts
            return []

        monkeypatch.setattr("app.history.upbit.fetch_seconds_range", fake_fetch)
        monkeypatch.setattr("app.history.binance.fetch_1s_range", fake_fetch)

        now = DAY_TS + 100
        await service.sync_upbit(db, "BTC", now_ts=now)
        assert captured["end_ts"] == now  # exclusive → 마지막 수집 초는 now-1
        await service.sync_binance(db, "BTC", now_ts=now)
        assert captured["end_ts"] == now

    async def test_cursor_never_regresses(self, db) -> None:
        """낡은 관측이 나중에 커밋돼도 커서가 과거로 되돌아가지 않는다."""
        await store.set_cursor(db, "upbit", "BTC", DAY_TS + 100, "101")
        await store.set_cursor(db, "upbit", "BTC", DAY_TS + 50, "100")  # 과거 값

        cursor = await store.get_cursor(db, "upbit", "BTC")
        await db.refresh(cursor)
        assert cursor.last_ts == DAY_TS + 100
        assert cursor.last_price == "101"


class TestFxObservation:
    """record_fx_observation — refresh 와 sync 의 공용 환율 반영 경로."""

    async def test_live_row_updated_even_when_cursor_is_current(self, db) -> None:
        """백필이 커서만 전진시킨 상태(주말 등)에서도 라이브 환율 행이 만들어진다."""
        from app.db import repository
        from app.history.hana import FxObservation

        # 백필이 한 일: 커서는 최신인데 fx_rate 라이브 행은 없다.
        await store.set_cursor(db, "fx", "USD", DAY_TS + 100, "1418.4")

        obs = FxObservation(
            ts=DAY_TS + 100, rate=Decimal("1418.4"), round_no=700, basis_date=DAY
        )
        changed = await service.record_fx_observation(db, obs)

        assert changed is False  # 이력에는 새 변동이 없지만
        row = await repository.get_fx_rate(db)
        assert row is not None and row.rate == 1418.4  # 라이브 행은 채워졌다

    async def test_pack_dedups_adjacent_equal_prices(self, db) -> None:
        """백필·sync 겹침으로 같은 가격 이벤트가 인접해도 팩킹이 정리한다."""
        await store.add_price_points(
            db,
            "upbit",
            "BTC",
            [(DAY_TS, "100"), (DAY_TS + 60, "100"), (DAY_TS + 120, "101")],
        )
        await service.pack_price_days(db, "upbit", "BTC", now_ts=NOW_TS)
        await db.commit()

        events = await service.load_price_events(
            db, "upbit", "BTC", DAY_TS, DAY_TS + 86_400
        )
        # (DAY_TS+60, "100") 은 직전과 같은 가격 — 변동이 아니므로 제거된다.
        assert [(ts, str(p)) for ts, p in events] == [
            (DAY_TS, "100"),
            (DAY_TS + 120, "101"),
        ]
