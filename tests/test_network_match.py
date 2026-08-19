"""네트워크 맞추기 테스트 — 실제 거래소 응답에서 관측된 이름들로 짠다.

여기 나오는 이름은 전부 2026-08-19 에 세 거래소 API 에서 그대로 받은 값이다.
(네트워크 불필요 — 관측값을 상수로 박아 재현한다)
"""

from __future__ import annotations

from app.exchanges.private.network_match import Verdict, choose, find, tokens
from app.exchanges.private.wallet_status import NetworkStatus


def net(code: str, name: str, dep: bool = True, wd: bool = True) -> NetworkStatus:
    return NetworkStatus(code=code, name=name, deposit=dep, withdrawal=wd)


class TestTokens:
    def test_drops_parenthetical_and_decoration(self) -> None:
        assert tokens("Ethereum (ERC20)") == tokens("Ethereum")
        assert tokens("Polygon POS") == tokens("Polygon")
        assert tokens("Sonic Network") == tokens("Sonic")

    def test_applies_token_alias(self) -> None:
        assert tokens("Avalanche C-Chain") == tokens("AVAX C-Chain")

    def test_is_order_free(self) -> None:
        assert tokens("Asset Hub Polkadot") == tokens("Polkadot Asset Hub Chain")


class TestFind:
    def test_matches_by_code_first(self) -> None:
        v, hit = find(net("ETH", "Ethereum"), (net("ETH", "Ethereum (ERC20)"),))
        assert v is Verdict.MATCHED and hit is not None and hit.code == "ETH"

    def test_matches_by_name_when_codes_differ(self) -> None:
        """업비트 BASENET · 빗썸 BASE_ETH · 바이낸스 BASE — 전부 같은 Base 다."""
        v, hit = find(net("BASENET", "Base"), (net("BASE", "Base"),))
        assert v is Verdict.MATCHED and hit is not None and hit.code == "BASE"

    def test_matches_across_token_boundary(self) -> None:
        """붙여 쓴 이름과 띄어 쓴 이름은 같은 체인이다."""
        v, hit = find(
            net("DOT", "AssetHub Polkadot"),
            (net("BSC", "BNB Smart Chain (BEP20)"), net("STATEMINT", "Asset Hub Polkadot")),
        )
        assert v is Verdict.MATCHED and hit is not None and hit.code == "STATEMINT"

    def test_matches_curated_alias(self) -> None:
        v, hit = find(net("METAL", "Metal L2"), (net("MTL", "Metal DAO L2"),))
        assert v is Verdict.MATCHED and hit is not None

    def test_absent_when_nothing_overlaps(self) -> None:
        """QuarkChain 네이티브와 ERC20 은 다른 체인이다 — 옮길 수 없다."""
        v, hit = find(net("QKC", "Quarkchain"), (net("ETH", "Ethereum (ERC20)"),))
        assert v is Verdict.ABSENT and hit is None

    def test_unknown_when_names_partly_overlap(self) -> None:
        """**가장 중요한 케이스** — 업비트 Sei(네이티브) 와 바이낸스 SEIEVM.

        토큰이 겹치지만 주소 체계가 다르다. 부분 일치로 맞춰버리면 못 옮기는
        코인이 "가능"으로 돌아온다. 확신할 수 없으면 UNKNOWN 이어야 한다.
        """
        v, hit = find(net("SEI", "Sei"), (net("SEIEVM", "Sei EVM"),))
        assert v is Verdict.UNKNOWN and hit is None

    def test_unknown_when_foreign_has_no_network_info(self) -> None:
        """정보가 없는 것과 '그 망이 없는 것'은 다르다."""
        v, hit = find(net("ETH", "Ethereum"), ())
        assert v is Verdict.UNKNOWN and hit is None


class TestChoose:
    def test_prefers_a_network_that_can_actually_move(self) -> None:
        """국내가 여러 망을 지원하면 **옮길 수 있는 길**을 고른다."""
        domestic = (
            net("ETH", "Ethereum", dep=True, wd=True),
            net("TRX", "Tron", dep=True, wd=True),
        )
        foreign = (
            net("ETH", "Ethereum (ERC20)", dep=True, wd=False),  # 출금 막힘
            net("TRX", "Tron", dep=True, wd=True),               # 이쪽은 열림
        )
        dom, verdict, fx = choose(domestic, foreign)
        assert verdict is Verdict.MATCHED
        assert dom is not None and dom.code == "TRX"
        assert fx is not None and fx.code == "TRX"

    def test_falls_back_to_a_matched_network_even_if_closed(self) -> None:
        """옮길 수 있는 길이 없으면, 맞는 망을 그대로 알려준다 (막힘으로 보이게)."""
        dom, verdict, fx = choose(
            (net("ETH", "Ethereum"),), (net("ETH", "Ethereum (ERC20)", wd=False),)
        )
        assert verdict is Verdict.MATCHED
        assert fx is not None and fx.withdrawal is False

    def test_unknown_when_domestic_networks_missing(self) -> None:
        dom, verdict, fx = choose((), (net("ETH", "Ethereum"),))
        assert dom is None and verdict is Verdict.UNKNOWN and fx is None


class TestSpreadTransferState:
    """``/spreads`` 가 네트워크까지 보고 판정하는지 — 서비스 레벨."""

    @staticmethod
    def _rows(dom_nets, fx_nets):
        from conftest import FX_RATE, snapshot_row

        dom = snapshot_row("upbit", "BTC", 100_000_000.0)
        fx = snapshot_row(
            "binance", "BTC", 50_000.0, quote="USDT", krw_factor=FX_RATE
        )
        dom.networks = dom_nets
        fx.networks = fx_nets
        return dom, fx

    async def _get(self, client, db, dom_nets, fx_nets):
        from conftest import seed_rows, seed_usdkrw_rate

        dom, fx = self._rows(dom_nets, fx_nets)
        await seed_rows(db, "upbit", [dom])
        await seed_rows(db, "binance", [fx])
        await seed_usdkrw_rate(db)
        return (await client.get("/spreads")).json()["rows"][0]

    async def test_uses_the_matched_network_not_the_coin_level_value(
        self, client, db
    ) -> None:
        """GRT 실사례 — 바이낸스는 Arbitrum 출금이 열려 코인 단위로는 '가능'
        이지만, 업비트는 Ethereum 으로만 받고 그 망 출금은 막혀 있다.
        **옮길 수 없으므로 '가능'이라고 하면 안 된다.**
        """
        row = await self._get(
            client,
            db,
            [{"code": "ETH", "name": "Ethereum", "dep": True, "wd": True}],
            [
                {"code": "ARBITRUM", "name": "Arbitrum One", "dep": True, "wd": True},
                {"code": "ETH", "name": "Ethereum (ERC20)", "dep": True, "wd": False},
            ],
        )
        assert row["wdFx"] is False, "Arbitrum 이 열렸다고 옮길 수 있는 게 아니다"
        assert row["netDom"] == "Ethereum"

    async def test_absent_network_is_blocked(self, client, db) -> None:
        """QKC 실사례 — 국내는 네이티브, 해외는 ERC20 뿐. 옮길 길이 없다."""
        row = await self._get(
            client,
            db,
            [{"code": "QKC", "name": "Quarkchain", "dep": True, "wd": True}],
            [{"code": "ETH", "name": "Ethereum (ERC20)", "dep": True, "wd": True}],
        )
        assert row["depFx"] is False and row["wdFx"] is False
        assert row["netDom"] == "Quarkchain"

    async def test_ambiguous_network_is_unknown_not_open(self, client, db) -> None:
        """**가장 중요** — 확신 못 하면 '가능'이 아니라 '확인 불가'다.

        여기서 코인 단위 값으로 접으면 걷어낸 낙관 편향이 그대로 돌아온다.
        """
        row = await self._get(
            client,
            db,
            [{"code": "SEI", "name": "Sei", "dep": True, "wd": True}],
            [{"code": "SEIEVM", "name": "Sei EVM", "dep": True, "wd": True}],
        )
        assert row["wdFx"] is None and row["depFx"] is None

    async def test_domestic_state_comes_from_the_chosen_network(
        self, client, db
    ) -> None:
        row = await self._get(
            client,
            db,
            [{"code": "ETH", "name": "Ethereum", "dep": False, "wd": True}],
            [{"code": "ETH", "name": "Ethereum (ERC20)", "dep": True, "wd": True}],
        )
        assert row["depDom"] is False and row["wdDom"] is True

    async def test_falls_back_to_coin_level_when_no_network_info(
        self, client, db
    ) -> None:
        """망 정보가 없던 시절 데이터로도 예전만큼은 동작해야 한다."""
        row = await self._get(client, db, [], [])
        assert row["depDom"] is True and row["wdFx"] is True
        assert row["netDom"] is None


class TestNetworksSurviveCollection:
    """수집 → 메모리 → 조회까지 네트워크가 실려 가는가.

    이 테스트가 없어서 LiveSnapshot 에 networks 를 안 옮긴 걸 놓쳤다.
    스냅샷을 직접 심는 테스트만 있으면 수집 경로의 누락이 안 잡힌다.
    """

    async def test_collector_carries_networks_into_live_store(
        self, db, monkeypatch
    ) -> None:
        from conftest import refresh_once

        from app.exchanges.private.wallet_status import WalletStatus
        from app.services.collector_service import CollectorService
        from app.services.live_store import live_store

        service = CollectorService()
        await refresh_once(
            service,
            db,
            monkeypatch,
            domestic_bases=["BTC"],
            binance_bases=["BTC"],
            wallet_status={
                "BTC": WalletStatus(
                    deposit=True,
                    withdrawal=True,
                    networks=(net("BTC", "Bitcoin"),),
                )
            },
        )

        snap = live_store.get_snapshot("upbit", "BTC")
        assert snap is not None
        assert snap.networks == [
            {"code": "BTC", "name": "Bitcoin", "dep": True, "wd": True}
        ]

    async def test_networks_survive_db_round_trip(self, db) -> None:
        from conftest import seed_rows, snapshot_row

        from app.db import repository

        row = snapshot_row("upbit", "BTC", 1.0)
        row.networks = [{"code": "BTC", "name": "Bitcoin", "dep": True, "wd": False}]
        await seed_rows(db, "upbit", [row])

        got = (await repository.get_snapshots(db, exchange="upbit"))[0]
        assert got.networks == [
            {"code": "BTC", "name": "Bitcoin", "dep": True, "wd": False}
        ]


class TestSchemaMigration:
    """``networks`` 는 기존 테이블에 **나중에** 붙은 컬럼이다.

    create_all 은 이미 있는 테이블을 건드리지 않으므로, 컬럼을 붙이는 DDL 이
    없으면 배포된 DB 에서 저장만 조용히 실패한다 (조회는 메모리를 보므로
    멀쩡해 보인다 — 그래서 눈으로는 안 잡힌다). 실제로 한 번 냈던 사고다.
    """

    def test_cleanup_ddl_adds_networks_column(self) -> None:
        from app.db.views import CLEANUP_DDL

        flat = [" ".join(d.split()) for d in CLEANUP_DDL]
        assert any(
            "ADD COLUMN IF NOT EXISTS networks" in d for d in flat
        ), "networks 컬럼을 붙이는 DDL 이 없다 — 기존 DB 에서 저장이 실패한다"
