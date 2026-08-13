"""매트릭스 서비스 테스트 — 코인별 최대 김프·역프 조합 (네트워크 불필요)."""

from __future__ import annotations

import pytest
from conftest import (
    BINANCE_PRICES,
    BITHUMB_PRICES,
    FX_RATE,
    UPBIT_PRICES,
    fwd_execution_percent,
    rev_execution_percent,
    seed_fx_rate,
    seed_rows,
    seed_standard,
    snapshot_row,
)

from app.core.config import settings
from app.core.errors import MarketDataNotFoundError
from app.db.repository import SnapshotRow
from app.services.matrix_service import matrix_service


class TestPreconditions:
    async def test_empty_db_raises(self, db) -> None:
        with pytest.raises(MarketDataNotFoundError):
            await matrix_service.build(db, amount_krw=1_000_000.0)

    async def test_missing_rates_raise(self, db) -> None:
        await seed_rows(db, "upbit", [snapshot_row("upbit", "BTC", 100_000_000.0)])
        with pytest.raises(MarketDataNotFoundError):
            await matrix_service.build(db, amount_krw=1_000_000.0)


class TestBestCombinationSelection:
    """조합마다 김프·역프를 계산해 방향별 최대 조합을 고른다."""

    async def test_universe_and_counts(self, db) -> None:
        await seed_standard(db)
        res = await matrix_service.build(db, amount_krw=1_000_000.0)

        # 국내 ∩ 해외 = BTC, ETH, XRP (SOL 은 국내 미상장)
        assert res.scanned_coins == 3
        assert {c.sym for c in res.coins} == {"BTC", "ETH", "XRP"}
        # 조합 수: BTC 2(국내)×1, ETH 1×1, XRP 2×1 = 5
        assert res.scanned_combinations == 5
        assert res.dom_list == ["bithumb", "upbit"]
        assert res.fx_list == ["binance"]
        assert res.amount_krw == 1_000_000.0

    async def test_kimchi_picks_largest_premium_combo(self, db) -> None:
        """BTC 김프는 (바이낸스 매수 → 빗썸 매도) 조합이 가장 크다."""
        await seed_standard(db)
        res = await matrix_service.build(db, amount_krw=1_000_000.0)

        btc = next(c for c in res.coins if c.sym == "BTC")
        assert btc.fwd is not None
        assert btc.fwd.buy_exchange == "binance"
        assert btc.fwd.sell_exchange == "bithumb"

        # 빗썸 조합은 빗썸 자기 환율(1401)로 계산된다
        expected = fwd_execution_percent(
            BITHUMB_PRICES["BTC"], BINANCE_PRICES["BTC"], FX_RATE
        )
        assert btc.fwd.premium_percent == pytest.approx(expected)

        # 확인 사살: 업비트 조합보다 커야 한다
        upbit_combo = fwd_execution_percent(
            UPBIT_PRICES["BTC"], BINANCE_PRICES["BTC"], FX_RATE
        )
        assert btc.fwd.premium_percent > upbit_combo

    async def test_reverse_picks_its_own_combo(self, db) -> None:
        """역프 1등 조합은 김프 1등과 다를 수 있다 — BTC 는 (업비트 → 바이낸스)."""
        await seed_standard(db)
        res = await matrix_service.build(db, amount_krw=1_000_000.0)

        btc = next(c for c in res.coins if c.sym == "BTC")
        assert btc.rev is not None
        assert btc.rev.buy_exchange == "upbit"
        assert btc.rev.sell_exchange == "binance"

        expected = rev_execution_percent(
            UPBIT_PRICES["BTC"], BINANCE_PRICES["BTC"], FX_RATE
        )
        assert btc.rev.premium_percent == pytest.approx(expected)
        # 국내가 비싼 시나리오라 역프는 손해다
        assert btc.rev.premium_percent < 0

    async def test_sorted_by_kimchi_premium_desc(self, db) -> None:
        """시드에서 XRP 김프가 가장 크다 → 첫 행은 XRP."""
        await seed_standard(db)
        res = await matrix_service.build(db, amount_krw=1_000_000.0)

        assert res.coins[0].sym == "XRP"
        percents = [c.fwd.premium_percent for c in res.coins]
        assert percents == sorted(percents, reverse=True)

class TestEffectiveAndSlippage:
    async def test_small_amount_realizes_full_premium(self, db) -> None:
        await seed_standard(db)
        res = await matrix_service.build(db, amount_krw=1_000_000.0)

        btc = next(c for c in res.coins if c.sym == "BTC")
        # 1단계 안에서 끝난다 → 슬리피지 0
        assert btc.fwd.total_slippage_percent == pytest.approx(0.0)
        assert btc.fwd.depth_exhausted is False

    async def test_slippage_is_never_negative(self, db) -> None:
        await seed_standard(db)
        res = await matrix_service.build(db, amount_krw=10_000_000.0)

        for coin in res.coins:
            for direction in (coin.fwd, coin.rev):
                if direction is None:
                    continue
                assert direction.total_slippage_percent >= -1e-9

    async def test_depth_exhausted_when_amount_exceeds_stored_book(self, db) -> None:
        """저장 호가는 코인당 약 1,500만원어치 — 그 이상은 소진 표시."""
        await seed_standard(db)
        res = await matrix_service.build(db, amount_krw=50_000_000.0)

        btc = next(c for c in res.coins if c.sym == "BTC")
        assert btc.fwd.depth_exhausted is True

    async def test_amount_above_storage_limit_adds_warning(self, db) -> None:
        await seed_standard(db)
        over = settings.orderbook_max_amount_krw * 2
        res = await matrix_service.build(db, amount_krw=over)

        assert any("호가 저장 한도" in w for w in res.warnings)


class TestWalletFlags:
    async def test_flags_follow_buy_and_sell_sides(self, db) -> None:
        """김프는 구매처 출금·판매처 입금, 역프는 그 반대 축을 본다."""
        await seed_rows(
            db,
            "upbit",
            [
                snapshot_row(
                    "upbit", "BTC", 100_000_000.0, deposit=True, withdrawal=False
                )
            ],
        )
        await seed_rows(
            db,
            "binance",
            [
                snapshot_row(
                    "binance",
                    "BTC",
                    71_000.0,
                    quote="USDT",
                    krw_factor=1400,
                    deposit=False,
                    withdrawal=True,
                )
            ],
        )
        await seed_fx_rate(db)

        res = await matrix_service.build(db, amount_krw=1_000_000.0)
        btc = res.coins[0]

        # 김프: 바이낸스 매수(출금 True) → 업비트 매도(입금 True)
        assert btc.fwd.withdrawal_available is True
        assert btc.fwd.deposit_available is True
        # 역프: 업비트 매수(출금 False) → 바이낸스 매도(입금 False)
        assert btc.rev.withdrawal_available is False
        assert btc.rev.deposit_available is False

    async def test_unknown_wallet_status_propagates_as_null(self, db) -> None:
        """빗썸 입출금 상태를 모르는 표준 시드 — 판매처 입금 플래그가 null."""
        await seed_standard(db)
        res = await matrix_service.build(db, amount_krw=1_000_000.0)

        btc = next(c for c in res.coins if c.sym == "BTC")
        assert btc.fwd.sell_exchange == "bithumb"
        assert btc.fwd.deposit_available is None
        # 주의: 현재 구현은 withdrawal_available 이 null 일 때만 경고를 붙인다.
        # deposit_available 만 null 인 이 시나리오에서는 경고가 없다 (앱 코드의 비대칭).

    async def test_null_withdrawal_flag_adds_warning(self, db) -> None:
        """구매처 출금 가능 여부를 모르면(null) 경고가 붙는다."""
        await seed_rows(db, "upbit", [snapshot_row("upbit", "BTC", 100_000_000.0)])
        await seed_rows(
            db,
            "binance",
            [
                snapshot_row(
                    "binance",
                    "BTC",
                    71_000.0,
                    quote="USDT",
                    krw_factor=1400,
                    deposit=None,
                    withdrawal=None,
                )
            ],
        )
        await seed_fx_rate(db)

        res = await matrix_service.build(db, amount_krw=1_000_000.0)
        btc = res.coins[0]
        assert btc.fwd.withdrawal_available is None  # 구매처 = 바이낸스
        assert any("입출금 가능 여부" in w for w in res.warnings)


class TestEdgeCases:
    async def test_coin_with_empty_book_is_skipped(self, db) -> None:
        """호가가 비어 계산 불가능한 코인은 행에서 빠진다."""
        await seed_rows(db, "upbit", [snapshot_row("upbit", "BTC", 100_000_000.0)])
        await seed_rows(
            db,
            "binance",
            [
                SnapshotRow(
                    exchange="binance",
                    base="BTC",
                    native_symbol="BTCUSDT",
                    quote="USDT",
                    price=71_000.0,
                    asks=[],
                    bids=[],
                )
            ],
        )
        await seed_fx_rate(db)

        res = await matrix_service.build(db, amount_krw=1_000_000.0)
        assert res.coins == []
        assert res.scanned_coins == 0
        assert res.scanned_combinations == 1  # 조합은 시도했지만 계산 불가

    async def test_excluded_bases_are_skipped(self, db, monkeypatch) -> None:
        await seed_standard(db)
        monkeypatch.setattr(settings, "scan_excluded_bases", ["XRP"])

        res = await matrix_service.build(db, amount_krw=1_000_000.0)
        assert {c.sym for c in res.coins} == {"BTC", "ETH"}

    async def test_suspicious_flag_on_abnormal_premium(self, db) -> None:
        """±5% 이상 김프는 의심(티커 충돌 등) 표시."""
        await seed_rows(db, "upbit", [snapshot_row("upbit", "AI", 168_000.0)])
        await seed_rows(
            db,
            "binance",
            [snapshot_row("binance", "AI", 100.0, quote="USDT", krw_factor=1400)],
        )
        await seed_fx_rate(db)

        res = await matrix_service.build(db, amount_krw=1_000_000.0)
        assert res.coins[0].suspicious is True

    async def test_data_freshness_fields_are_filled(self, db) -> None:
        await seed_standard(db)
        res = await matrix_service.build(db, amount_krw=1_000_000.0)

        assert res.data_oldest_at is not None
        assert res.data_newest_at is not None
        assert res.data_oldest_at <= res.data_newest_at
