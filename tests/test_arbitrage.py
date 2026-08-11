"""금액 기준 차익 시뮬레이션 테스트 — DB 스냅샷 기반 (네트워크 불필요)."""

from __future__ import annotations

import pytest
from conftest import (
    BINANCE_PRICES,
    BITHUMB_PRICES,
    KRW_RATES,
    best_ask,
    best_bid,
    seed_rates,
    seed_rows,
    seed_standard,
    snapshot_row,
)

from app.core.errors import (
    InvalidRequestError,
    MarketDataNotFoundError,
    NoArbitrageOpportunityError,
    UnsupportedExchangeError,
)
from app.db.repository import SnapshotRow
from app.models.premium import PremiumDirection
from app.services.arbitrage_service import arbitrage_service


class TestValidation:
    async def test_invalid_currency_raises(self, db) -> None:
        with pytest.raises(InvalidRequestError):
            await arbitrage_service.simulate(
                db, "BTC", amount=1_000_000.0, currency="EUR"
            )

    async def test_empty_db_raises(self, db) -> None:
        with pytest.raises(MarketDataNotFoundError):
            await arbitrage_service.simulate(db, "BTC", amount=1_000_000.0)

    async def test_missing_rates_raise(self, db) -> None:
        await seed_rows(db, "upbit", [snapshot_row("upbit", "BTC", 100_000_000.0)])
        with pytest.raises(MarketDataNotFoundError):
            await arbitrage_service.simulate(db, "BTC", amount=1_000_000.0)

    async def test_unknown_exchange_raises(self, db) -> None:
        await seed_standard(db)
        with pytest.raises(UnsupportedExchangeError):
            await arbitrage_service.simulate(
                db, "BTC", amount=1_000_000.0, exchanges=["coinbase"]
            )

    async def test_single_venue_is_409_style(self, db) -> None:
        await seed_rows(db, "upbit", [snapshot_row("upbit", "BTC", 100_000_000.0)])
        await seed_rates(db, {"upbit": 1400.0})
        with pytest.raises(NoArbitrageOpportunityError):
            await arbitrage_service.simulate(db, "BTC", amount=1_000_000.0)


class TestAutoDirection:
    """방향 생략 시 가장 싼 곳 ↔ 가장 비싼 곳을 자동 선택한다."""

    async def test_picks_profitable_pair(self, db) -> None:
        await seed_standard(db)
        res = await arbitrage_service.simulate(db, "btc", amount=1_000_000.0)

        # 최저 매도호가 = 바이낸스(99.45M 환산), 최고 매수호가 = 빗썸(100.05M)
        assert res.sym == "BTC"
        assert res.direction is None
        assert res.buy.exchange == "binance"
        assert res.sell.exchange == "bithumb"
        assert res.profit_krw > 0
        assert res.profit_percent > 0

    async def test_surface_premium_matches_top_of_book(self, db) -> None:
        await seed_standard(db)
        res = await arbitrage_service.simulate(db, "BTC", amount=1_000_000.0)

        buy_krw = best_ask(BINANCE_PRICES["BTC"]) * KRW_RATES["upbit"]
        sell_krw = best_bid(BITHUMB_PRICES["BTC"])
        assert res.premium_percent == pytest.approx((sell_krw / buy_krw - 1) * 100)
        # 소액이라 1단계 안에서 끝난다 → 슬리피지 0, 프리미엄을 그대로 다 먹는다
        assert res.profit_percent == pytest.approx(res.premium_percent)
        assert res.premium_capture_percent == pytest.approx(100.0)

    async def test_candidates_are_sorted_by_best_ask(self, db) -> None:
        await seed_standard(db)
        res = await arbitrage_service.simulate(db, "BTC", amount=1_000_000.0)

        assert [c.exchange for c in res.candidates] == ["binance", "upbit", "bithumb"]

    async def test_larger_amount_reduces_capture(self, db) -> None:
        """금액이 커지면 호가를 파고들어 프리미엄 대비 실현율이 떨어진다."""
        await seed_standard(db)
        small = await arbitrage_service.simulate(db, "BTC", amount=1_000_000.0)
        large = await arbitrage_service.simulate(db, "BTC", amount=10_000_000.0)

        assert large.profit_percent < small.profit_percent
        assert large.premium_capture_percent < small.premium_capture_percent
        assert large.buy.levels_consumed > 1

    async def test_same_exchange_both_sides_is_409_style(self, db) -> None:
        """한 거래소가 최저 매도·최고 매수를 동시에 갖으면 차익 기회가 없다."""
        await seed_rows(
            db,
            "upbit",
            [
                SnapshotRow(
                    exchange="upbit",
                    base="BTC",
                    native_symbol="KRW-BTC",
                    quote="KRW",
                    price=100_000_000.0,
                    # 스프레드가 아주 넓은 국내 호가
                    asks=[[101_000_000.0, 1.0]],
                    bids=[[99_000_000.0, 1.0]],
                )
            ],
        )
        await seed_rows(
            db,
            "binance",
            [snapshot_row("binance", "BTC", 71_000.0, quote="USDT", krw_factor=1400)],
        )
        await seed_rates(db, {"upbit": 1400.0})

        with pytest.raises(NoArbitrageOpportunityError):
            await arbitrage_service.simulate(db, "BTC", amount=1_000_000.0)

    async def test_depth_exhausted_adds_warning(self, db) -> None:
        """저장 호가(단계당 300만원 × 5단계)를 넘는 금액은 소진 경고가 붙는다."""
        await seed_standard(db)
        res = await arbitrage_service.simulate(db, "BTC", amount=100_000_000.0)

        assert res.buy.depth_exhausted is True
        assert any("소진" in w for w in res.warnings)


class TestFixedDirection:
    async def test_kimchi_buys_overseas_sells_domestic(self, db) -> None:
        await seed_standard(db)
        res = await arbitrage_service.simulate(
            db, "BTC", amount=1_000_000.0, direction=PremiumDirection.FWD
        )

        assert res.direction is PremiumDirection.FWD
        assert res.buy.exchange == "binance"  # 해외에서 산다
        assert res.sell.exchange == "bithumb"  # 국내 최고 매수호가
        assert res.profit_krw > 0

    async def test_reverse_can_lose_and_warns(self, db) -> None:
        """방향을 고정하면 손해(음수)가 그대로 나온다 — 그게 정상이다."""
        await seed_standard(db)
        res = await arbitrage_service.simulate(
            db, "BTC", amount=1_000_000.0, direction=PremiumDirection.REV
        )

        assert res.direction is PremiumDirection.REV
        assert res.buy.exchange in ("upbit", "bithumb")  # 국내에서 산다
        assert res.sell.exchange == "binance"  # 해외에서 판다
        assert res.profit_krw < 0  # 국내가 비싼 시나리오라 역방향은 손해
        assert any("손해" in w for w in res.warnings)

    async def test_direction_with_exchanges_auto_includes_domestic(self, db) -> None:
        """direction 지정 시 exchanges 는 해외 목록으로 해석되고 국내는 자동 포함."""
        await seed_standard(db)
        res = await arbitrage_service.simulate(
            db,
            "BTC",
            amount=1_000_000.0,
            direction=PremiumDirection.FWD,
            exchanges=["binance"],
        )

        assert res.buy.exchange == "binance"
        assert res.sell.exchange in ("upbit", "bithumb")  # 국내가 자동으로 살아있다

    async def test_direction_without_domestic_snapshot_is_409_style(self, db) -> None:
        await seed_rows(
            db,
            "binance",
            [snapshot_row("binance", "BTC", 71_000.0, quote="USDT", krw_factor=1400)],
        )
        # 해외 하나 + KRW 스냅샷 없음 → 방향 고정 계산 불가. 다만 후보 2곳 미만
        # 검사가 먼저라 NoArbitrageOpportunityError 로 걸린다.
        await seed_rates(db, {"upbit": 1400.0})
        with pytest.raises(NoArbitrageOpportunityError):
            await arbitrage_service.simulate(
                db, "BTC", amount=1_000_000.0, direction=PremiumDirection.FWD
            )


class TestCurrencyConversion:
    async def test_usdt_input(self, db) -> None:
        """USDT 를 넣으면 모든 호가를 USDT 로 환산해 비교·체결한다."""
        await seed_standard(db)
        res = await arbitrage_service.simulate(
            db, "BTC", amount=1_000.0, currency="USDT"
        )

        # 원화 환산은 기준(업비트) 환율
        assert res.input_amount_krw == pytest.approx(1_000.0 * KRW_RATES["upbit"])
        assert res.buy.exchange == "binance"
        assert res.profit_percent > 0

    async def test_amounts_are_reported_in_krw(self, db) -> None:
        await seed_standard(db)
        res = await arbitrage_service.simulate(db, "BTC", amount=1_000_000.0)

        # 투입 1,000,000원이 전부 체결된다 (1단계 잔량 안)
        assert res.buy.amount_krw == pytest.approx(1_000_000.0)

    async def test_explicit_exchange_without_snapshot_is_partial_failure(
        self, db
    ) -> None:
        """빗썸에 없는 ETH — 요청에 넣으면 failures 로 기록하고 나머지로 계산한다."""
        await seed_standard(db)
        res = await arbitrage_service.simulate(
            db,
            "ETH",
            amount=1_000_000.0,
            exchanges=["upbit", "bithumb", "binance"],
        )

        assert [f.exchange for f in res.failures] == ["bithumb"]
        assert {res.buy.exchange, res.sell.exchange} == {"binance", "upbit"}
        assert res.profit_krw > 0  # ETH 도 김프 양수 시나리오
