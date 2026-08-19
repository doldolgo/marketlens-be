"""입출금 3-state 계약 테스트 — "확인 불가"는 "막힘"이 아니다.

값의 뜻 (``app.db.models.MarketSnapshot`` 과 같다):

    True  : 확인했고 열려 있음
    False : 확인했고 막힘
    None  : 확인 불가 — 키 없음 · API 실패 · 응답에 그 코인이 없음

이 파일이 지키는 것은 하나다 — **None 이 "열림"으로 새지 않는다.**
모르는 경로를 옮길 수 있다고 말하면 실자본이 그 말을 믿고 움직인다.
"""

from __future__ import annotations

from conftest import (
    FX_RATE,
    refresh_once,
    seed_rows,
    seed_usdkrw_rate,
    snapshot_row,
)

from app.db import repository
from app.db.views import CLEANUP_DDL
from app.exchanges.private.wallet_status import WalletStatus
from app.models.bulk import BulkQuote
from app.services.arbitrage_service import arbitrage_service
from app.services.collector_service import CollectorService
from app.services.live_store import LiveRate, live_store
from app.services.matrix_service import matrix_service


class TestCollectionKeepsUnknown:
    """수집 단계 — 확인 못 한 것을 막혔다고 적지 않는다."""

    async def test_wallet_failure_yields_unknown_not_blocked(
        self, db, monkeypatch
    ) -> None:
        """지갑 조회가 실패하면 그 거래소 전 코인이 확인 불가여야 한다.

        여기서 False 로 적으면 거래소 API 장애 한 번이 "전 코인 입출금 중단"
        으로 둔갑한다 — 화면이 통째로 빨개지는 그 현상이다.
        """
        service = CollectorService()
        await refresh_once(
            service,
            db,
            monkeypatch,
            domestic_bases=["BTC"],
            binance_bases=["BTC"],
            wallet_fails=True,
        )

        rows = live_store.get_snapshots()
        assert rows, "수집은 계속돼야 한다 — 지갑 실패가 사이클을 죽이면 안 된다"
        for r in rows:
            assert r.deposit_enabled is None
            assert r.withdrawal_enabled is None

    async def test_coin_missing_from_wallet_response_is_unknown(
        self, db, monkeypatch
    ) -> None:
        """조회는 됐지만 응답에 그 코인이 없으면 그 코인만 확인 불가다."""
        service = CollectorService()
        await refresh_once(
            service,
            db,
            monkeypatch,
            domestic_bases=["BTC", "ETH"],
            binance_bases=["BTC", "ETH"],
            wallet_status={"BTC": WalletStatus(deposit=True, withdrawal=True)},
        )

        btc = live_store.get_snapshot("upbit", "BTC")
        eth = live_store.get_snapshot("upbit", "ETH")
        assert btc is not None and eth is not None
        assert btc.deposit_enabled is True
        assert eth.deposit_enabled is None, "응답에 없는 코인은 확인 불가다"

    async def test_all_three_states_survive_to_memory(self, db, monkeypatch) -> None:
        """True / False / None 이 수집을 지나 메모리까지 뭉개지지 않는다."""
        service = CollectorService()
        await refresh_once(
            service,
            db,
            monkeypatch,
            domestic_bases=["BTC", "ETH", "XRP"],
            binance_bases=["BTC", "ETH", "XRP"],
            wallet_status={
                "BTC": WalletStatus(deposit=True, withdrawal=True),
                "ETH": WalletStatus(deposit=False, withdrawal=False),
                # XRP 는 일부러 빠뜨린다 → 확인 불가
            },
        )

        got = {
            b: live_store.get_snapshot("upbit", b).deposit_enabled
            for b in ("BTC", "ETH", "XRP")
        }
        assert got == {"BTC": True, "ETH": False, "XRP": None}


class TestUnknownNeverReadsAsOpen:
    """**이 파일에서 가장 중요한 묶음** — 판단 지점마다 None 이 보수적인가."""

    def test_depth_targets_skip_unknown_domestic_deposit(self) -> None:
        """국내 입금이 확인 불가면 슬리피지 추적 대상에서 빠져야 한다.

        (``collector_service._select_depth_targets``)
        """
        service = CollectorService()
        tops = {
            "BTC": BulkQuote(
                base="BTC",
                quote="USDT",
                native_symbol="BTCUSDT",
                bid=99.9,
                bid_size=1.0,
                ask=100.0,
                ask_size=1.0,
            )
        }
        wallet_binance = {"BTC": WalletStatus(deposit=True, withdrawal=True)}
        # 김프가 크게 벌어진 국내 호가 (해외 100 USDT × 1400 = 14 만원 대비)
        rich = snapshot_row("upbit", "BTC", 200_000.0)

        def targets(deposit):
            row = snapshot_row("upbit", "BTC", 200_000.0, deposit=deposit)
            row.bids = rich.bids
            return service._select_depth_targets(
                {"upbit": {"BTC": row}},
                tops,
                {"upbit": LiveRate(exchange="upbit", ask=1400.0, bid=1400.0)},
                wallet_binance,
            )

        assert targets(True) == ["BTC"], "열려 있으면 대상이다 (대조군)"
        assert targets(False) == [], "막혔으면 제외"
        assert targets(None) == [], "확인 불가도 제외 — 열림으로 읽으면 안 된다"

    async def test_matrix_marks_unknown_and_warns(self, db) -> None:
        """매트릭스는 확인 불가를 null 로 전하고 경고를 남긴다."""
        await seed_rows(
            db,
            "upbit",
            [snapshot_row("upbit", "BTC", 100_000_000.0, deposit=None)],
        )
        await seed_rows(
            db,
            "binance",
            [
                snapshot_row(
                    "binance",
                    "BTC",
                    50_000.0,
                    quote="USDT",
                    krw_factor=FX_RATE,
                    withdrawal=None,
                )
            ],
        )
        await seed_usdkrw_rate(db)

        res = await matrix_service.build(db, amount_krw=1_000_000.0)
        fwd = res.coins[0].fwd
        assert fwd is not None
        assert fwd.deposit_available is None, "확인 불가는 True 로 접히면 안 된다"
        assert fwd.withdrawal_available is None
        assert any("입출금" in w for w in res.warnings)

    async def test_arbitrage_warns_on_unknown_instead_of_staying_silent(
        self, db
    ) -> None:
        """경고를 안 내면 "경고 없음 = 괜찮음"으로 읽힌다."""
        await seed_rows(
            db,
            "upbit",
            [snapshot_row("upbit", "BTC", 100_000_000.0, deposit=None)],
        )
        await seed_rows(
            db,
            "binance",
            [
                snapshot_row(
                    "binance",
                    "BTC",
                    50_000.0,
                    quote="USDT",
                    krw_factor=FX_RATE,
                    withdrawal=None,
                )
            ],
        )
        await seed_usdkrw_rate(db)

        res = await arbitrage_service.simulate(db, "BTC", amount=1_000_000.0)
        assert res.withdrawal_available is None
        assert res.deposit_available is None
        assert any("확인하지 못했" in w for w in res.warnings)


class TestRoundTrip:
    async def test_none_survives_db_round_trip(self, db) -> None:
        """None 으로 저장했다 읽어도 None 이다 (False 로 접히지 않는다)."""
        await seed_rows(
            db,
            "upbit",
            [
                snapshot_row("upbit", "BTC", 1.0, deposit=None, withdrawal=None),
                snapshot_row("upbit", "ETH", 1.0, deposit=False, withdrawal=True),
            ],
        )

        got = {
            s.base: (s.deposit_enabled, s.withdrawal_enabled)
            for s in await repository.get_snapshots(db, exchange="upbit")
        }
        assert got == {"BTC": (None, None), "ETH": (False, True)}


class TestRestartResilience:
    """1-4 의 지뢰 회귀 테스트.

    ``CLEANUP_DDL`` 은 ``init_db()`` 가 **기동마다** 실행한다. 여기에 NOT NULL
    을 거는 DDL 이 다시 들어오면, 스키마를 3-state 로 고쳐도 재기동 한 번에
    조용히 원상복구된다. (실행 자체는 PostgreSQL 전용이라 SQLite 인 이 테스트
    에서는 문 자체를 검사한다)
    """

    def test_cleanup_ddl_never_reimposes_not_null(self) -> None:
        for ddl in CLEANUP_DDL:
            flat = " ".join(ddl.split())
            if "deposit_enabled" in flat or "withdrawal_enabled" in flat:
                assert "SET NOT NULL" not in flat, f"NOT NULL 재도입: {flat}"
                assert "SET DEFAULT" not in flat, f"DEFAULT 재도입: {flat}"
                assert "IS NULL" not in flat, f"null 을 메우는 UPDATE 재도입: {flat}"

    def test_cleanup_ddl_drops_not_null(self) -> None:
        """create_all 은 기존 컬럼의 nullable 을 안 바꾼다 — 직접 풀어야 한다."""
        flat = [" ".join(d.split()) for d in CLEANUP_DDL]
        for col in ("deposit_enabled", "withdrawal_enabled"):
            assert any(
                f"ALTER COLUMN {col} DROP NOT NULL" in d for d in flat
            ), f"{col} 의 NOT NULL 을 푸는 DDL 이 없다"


class TestSpreadsCarriesThreeStates:
    """수집 → 저장 → ``GET /spreads`` 까지 세 상태가 살아 있는가.

    화면이 mock 을 쓸 수밖에 없던 이유가 이 필드의 부재였다. 메모리 경로와
    DB 폴백 **양쪽 다** 확인한다 — 한쪽만 맞으면 재기동 전후로 화면이 달라진다.
    """

    #: 국내는 입금 확인 불가 · 출금 열림 / 해외는 입금 막힘 · 출금 확인 불가
    EXPECTED = {"depDom": None, "wdDom": True, "depFx": False, "wdFx": None}

    @staticmethod
    def _rows():
        return [
            snapshot_row(
                "upbit", "BTC", 100_000_000.0, deposit=None, withdrawal=True
            ),
            snapshot_row(
                "binance",
                "BTC",
                50_000.0,
                quote="USDT",
                krw_factor=FX_RATE,
                deposit=False,
                withdrawal=None,
            ),
        ]

    async def test_db_path(self, client, db) -> None:
        dom, fx = self._rows()
        await seed_rows(db, "upbit", [dom])
        await seed_rows(db, "binance", [fx])
        await seed_usdkrw_rate(db)

        row = (await client.get("/spreads")).json()["rows"][0]
        assert {k: row[k] for k in self.EXPECTED} == self.EXPECTED

    async def test_memory_path(self, client, db) -> None:
        import time as _time

        from conftest import live_snapshot

        live_store.replace(
            [live_snapshot(r) for r in self._rows()],
            {
                eid: LiveRate(exchange=eid, ask=FX_RATE, bid=FX_RATE)
                for eid in ("upbit", "bithumb")
            },
            _time.time(),
        )

        row = (await client.get("/spreads")).json()["rows"][0]
        assert {k: row[k] for k in self.EXPECTED} == self.EXPECTED


class TestFailureMetricCountsQueryFailures:
    """``dw_fail_*`` 은 "막힌 코인"이 아니라 "조회 실패"를 센다.

    예전 정의는 "입출금 불가 코인이 하나라도 있으면 실패"였다. 코인이 300개면
    그중 하나는 늘 막혀 있어 비율이 항상 1.000 이었다 — 실제로 세 거래소 모두
    1686/1686 이었다. 코인이 늘수록 1.0 에 수렴하는 지표는 아무것도 말해주지
    않는다.
    """

    async def _dw_failed(self, db, monkeypatch, wallet_status=None, fails=False):
        service = CollectorService()
        await refresh_once(
            service,
            db,
            monkeypatch,
            domestic_bases=["BTC", "ETH"],
            binance_bases=["BTC", "ETH"],
            wallet_status=wallet_status,
            wallet_fails=fails,
        )
        return service._pending.dw_failed

    async def test_blocked_coin_is_not_a_failure(self, db, monkeypatch) -> None:
        """막힌 코인이 있는 것은 **정상적인 관측 결과**다."""
        failed = await self._dw_failed(
            db,
            monkeypatch,
            wallet_status={
                "BTC": WalletStatus(deposit=False, withdrawal=False),
                "ETH": WalletStatus(deposit=True, withdrawal=True),
            },
        )
        assert failed["upbit"] is False, "막혔다고 조회가 실패한 것은 아니다"

    async def test_unknown_state_is_a_failure(self, db, monkeypatch) -> None:
        """상태를 못 받아온 회차만 실패로 센다."""
        failed = await self._dw_failed(db, monkeypatch, fails=True)
        assert failed["upbit"] is True

    async def test_all_open_is_not_a_failure(self, db, monkeypatch) -> None:
        failed = await self._dw_failed(db, monkeypatch)
        assert failed["upbit"] is False

    async def test_rate_is_not_pinned_at_one(self, db) -> None:
        """실패·성공이 섞이면 비율이 0 과 1 사이에 놓인다 (지표가 살아 있다)."""
        for failed in (False, True, False, False):
            await repository.bump_platform_status(
                db,
                exchange="upbit",
                received_ts=1_700_000_000,
                spot_market_count=200,
                futures_market_count=0,
                dw_failed=failed,
            )
        await db.commit()

        row = (await repository.get_platform_statuses(db))[0]
        assert row.update_count == 4
        assert row.dw_fail_count == 1
        assert 0.0 < row.dw_fail_count / row.update_count < 1.0
