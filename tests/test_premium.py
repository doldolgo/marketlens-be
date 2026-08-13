"""김프 / 역김프 계산 로직 테스트 — DB 스냅샷 기반 (네트워크 불필요)."""

from __future__ import annotations

import pytest
from conftest import (
    FX_RATE,
    NOW_MS,
    best_ask,
    best_bid,
    fwd_execution_percent,
    rev_execution_percent,
    seed_fx_rate,
    seed_rows,
    seed_standard,
    snapshot_row,
)

from app.core.config import settings
from app.core.errors import (
    InvalidRequestError,
    MarketDataNotFoundError,
    UnsupportedExchangeError,
)
from app.db.models import MarketSnapshot
from app.models.premium import PremiumDirection
from app.models.ticker import PriceSide
from app.services.premium_service import (
    PremiumService,
    premium_service,
    resolve_side,
    snapshot_price,
)


def make_snap(
    exchange: str = "binance",
    base: str = "BTC",
    quote: str = "USDT",
    *,
    price: float = 100.0,
    bids: list | None = None,
    asks: list | None = None,
) -> MarketSnapshot:
    return MarketSnapshot(
        exchange=exchange,
        base=base,
        native_symbol=f"{base}{quote}" if quote == "USDT" else f"{quote}-{base}",
        quote=quote,
        price=price,
        bids=bids if bids is not None else [],
        asks=asks if asks is not None else [],
        price_timestamp=NOW_MS,
    )


class TestResolveSide:
    """매수/매도 → 어느 호가를 집을지."""

    def test_buy_uses_ask(self) -> None:
        """살 때는 매도호가에 체결된다."""
        assert resolve_side(is_buy=True) is PriceSide.ASK

    def test_sell_uses_bid(self) -> None:
        """팔 때는 매수호가에 체결된다."""
        assert resolve_side(is_buy=False) is PriceSide.BID


class TestSnapshotPrice:
    """스냅샷에서 최우선 호가를 뽑는다."""

    def snap(self) -> MarketSnapshot:
        return make_snap(price=100.5, bids=[[99.0, 1.0]], asks=[[101.0, 1.0]])

    def test_bid_and_ask(self) -> None:
        assert snapshot_price(self.snap(), PriceSide.BID) == 99.0
        assert snapshot_price(self.snap(), PriceSide.ASK) == 101.0

    def test_empty_book_returns_none(self) -> None:
        snap = make_snap(bids=[], asks=[])
        assert snapshot_price(snap, PriceSide.BID) is None
        assert snapshot_price(snap, PriceSide.ASK) is None


class TestBuildEntry:
    """방향별 프리미엄 계산식."""

    def setup_method(self) -> None:
        self.service = PremiumService()

    def build(self, direction, overseas_usdt: float, domestic_krw: float):
        return self.service._build_entry(
            make_snap(),
            overseas_usdt,
            domestic_krw,
            1000.0,  # 환율
            direction,
        )

    def test_kimchi_no_premium_when_equal(self) -> None:
        # 해외 100 USDT × 환율 1000 = 100,000원, 국내도 100,000원
        e = self.build(PremiumDirection.FWD, 100.0, 100_000.0)
        assert e.premium_percent == pytest.approx(0.0)
        assert e.profitable is False

    def test_kimchi_profitable_when_domestic_is_expensive(self) -> None:
        # 국내 105,000원에 팔고 해외 100,000원에 산다 → +5%
        e = self.build(PremiumDirection.FWD, 100.0, 105_000.0)
        assert e.premium_percent == pytest.approx(5.0)
        assert e.premium_krw == pytest.approx(5_000.0)
        assert e.profitable is True
        assert e.usd == pytest.approx(100.0)

    def test_kimchi_loss_when_domestic_is_cheap(self) -> None:
        e = self.build(PremiumDirection.FWD, 100.0, 95_000.0)
        assert e.premium_percent == pytest.approx(-5.0)
        assert e.profitable is False

    def test_reverse_profitable_when_overseas_is_expensive(self) -> None:
        # 국내 100,000원에 사서 해외 105,000원(105 USDT)에 판다 → +5%
        e = self.build(PremiumDirection.REV, 105.0, 100_000.0)
        assert e.premium_percent == pytest.approx(5.0)
        assert e.profitable is True

    def test_directions_are_reciprocal_not_negation(self) -> None:
        """같은 가격이라도 김프 +5% 의 반대는 -5% 가 아니라 -4.76% 다."""
        fwd = self.build(PremiumDirection.FWD, 100.0, 105_000.0)
        rev = self.build(PremiumDirection.REV, 100.0, 105_000.0)

        assert fwd.premium_percent == pytest.approx(5.0)
        assert rev.premium_percent == pytest.approx(-100 / 21)  # ≈ -4.762%
        assert rev.premium_percent != pytest.approx(-fwd.premium_percent)

    def test_premium_krw_is_exact_mirror(self) -> None:
        """원화 절대 차익은 정확히 부호가 뒤집힌다 (비율과 달리)."""
        fwd = self.build(PremiumDirection.FWD, 100.0, 105_000.0)
        rev = self.build(PremiumDirection.REV, 100.0, 105_000.0)
        assert rev.premium_krw == pytest.approx(-fwd.premium_krw)


class TestResolveDomestic:
    def setup_method(self) -> None:
        self.service = PremiumService()

    def test_default_comes_from_settings(self) -> None:
        assert self.service.resolve_domestic(None) == settings.krw_reference_exchange

    @pytest.mark.parametrize("exchange_id", ["upbit", "bithumb"])
    def test_domestic_exchanges_are_selectable(self, exchange_id: str) -> None:
        assert self.service.resolve_domestic(exchange_id) == exchange_id

    def test_overseas_exchange_is_rejected(self) -> None:
        """바이낸스는 원화 거래소가 아니므로 국내 축이 될 수 없다."""
        with pytest.raises(InvalidRequestError):
            self.service.resolve_domestic("binance")

    def test_unknown_exchange_raises(self) -> None:
        with pytest.raises(UnsupportedExchangeError):
            self.service.resolve_domestic("coinbase")


class TestFetchPremiums:
    """DB 스냅샷으로 실제 계산 (in-memory SQLite)."""

    async def test_empty_db_raises_404_style(self, db) -> None:
        with pytest.raises(MarketDataNotFoundError):
            await premium_service.fetch_premiums(
                db, "BTC", direction=PremiumDirection.FWD
            )

    async def test_missing_rate_raises(self, db) -> None:
        await seed_rows(db, "upbit", [snapshot_row("upbit", "BTC", 100_000_000.0)])
        with pytest.raises(MarketDataNotFoundError):
            await premium_service.fetch_premiums(
                db, "BTC", direction=PremiumDirection.FWD
            )

    async def test_wrong_domestic_quote_raises(self, db) -> None:
        """국내 기준 거래소의 스냅샷이 KRW 마켓이 아니면 404 성격의 예외."""
        await seed_rows(
            db, "upbit", [snapshot_row("upbit", "BTC", 71_000.0, quote="USDT")]
        )
        await seed_fx_rate(db)
        with pytest.raises(MarketDataNotFoundError):
            await premium_service.fetch_premiums(
                db, "BTC", direction=PremiumDirection.FWD
            )

    async def test_kimchi_uses_execution_sides(self, db) -> None:
        await seed_standard(db)
        res = await premium_service.fetch_premiums(
            db, "btc", direction=PremiumDirection.FWD
        )

        assert res.sym == "BTC"
        assert res.dom == "upbit"
        assert res.usd_krw_rate == FX_RATE
        # 김프: 국내에서 팔므로 최우선 매수호가(bid)
        assert res.dom_price == pytest.approx(best_bid(100_000_000.0))

        assert [e.fx for e in res.premiums] == ["binance"]
        expected = fwd_execution_percent(100_000_000.0, 71_000.0, 1400.0)
        assert res.premiums[0].premium_percent == pytest.approx(expected)
        assert res.premiums[0].profitable is True

    async def test_reverse_is_negative_here(self, db) -> None:
        await seed_standard(db)
        res = await premium_service.fetch_premiums(
            db, "BTC", direction=PremiumDirection.REV
        )

        expected = rev_execution_percent(100_000_000.0, 71_000.0, 1400.0)
        assert res.premiums[0].premium_percent == pytest.approx(expected)
        assert res.premiums[0].profitable is False

    async def test_directions_use_opposite_sides(self, db) -> None:
        """김프는 국내 bid·해외 ask, 역김프는 국내 ask·해외 bid 를 쓴다."""
        await seed_standard(db)
        fwd = await premium_service.fetch_premiums(
            db, "BTC", direction=PremiumDirection.FWD
        )
        rev = await premium_service.fetch_premiums(
            db, "BTC", direction=PremiumDirection.REV
        )

        # 방향마다 다른 호가를 집으므로 국내가/해외 원화가가 서로 다르다
        assert fwd.dom_price == pytest.approx(best_bid(100_000_000.0))
        assert rev.dom_price == pytest.approx(best_ask(100_000_000.0))
        assert fwd.premiums[0].usd > rev.premiums[0].usd

    async def test_domestic_selection_uses_unified_rate(self, db) -> None:
        """어느 국내 거래소를 기준으로 하든 환율은 통일 환율 하나다."""
        await seed_standard(db)
        upbit = await premium_service.fetch_premiums(
            db, "BTC", direction=PremiumDirection.FWD
        )
        bithumb = await premium_service.fetch_premiums(
            db,
            "BTC",
            direction=PremiumDirection.FWD,
            domestic="bithumb",
        )

        assert bithumb.dom == "bithumb"
        assert bithumb.dom_price == pytest.approx(best_bid(100_100_000.0))
        assert bithumb.usd_krw_rate == FX_RATE
        assert bithumb.usd_krw_rate == upbit.usd_krw_rate  # 거래소별 환율은 없다

    async def test_missing_fx_rate_raises(self, db) -> None:
        """환율이 아직 수집되지 않았으면 계산 불가 — 404 성격의 예외."""
        await seed_rows(
            db, "bithumb", [snapshot_row("bithumb", "BTC", 100_100_000.0)]
        )
        with pytest.raises(MarketDataNotFoundError):
            await premium_service.fetch_premiums(
                db,
                "BTC",
                direction=PremiumDirection.FWD,
                domestic="bithumb",
            )

    async def test_explicit_exchange_without_snapshot_is_partial_failure(
        self, db
    ) -> None:
        """명시한 해외 거래소의 스냅샷이 없으면 failures 에 기록하고 계속한다."""
        await seed_rows(db, "upbit", [snapshot_row("upbit", "BTC", 100_000_000.0)])
        await seed_fx_rate(db)

        res = await premium_service.fetch_premiums(
            db,
            "BTC",
            direction=PremiumDirection.FWD,
            exchanges=["binance"],
        )
        assert res.premiums == []
        assert len(res.failures) == 1
        assert res.failures[0].exchange == "binance"
        assert res.failures[0].error_code == "market_data_not_found"

    async def test_unknown_overseas_exchange_raises(self, db) -> None:
        await seed_standard(db)
        with pytest.raises(UnsupportedExchangeError):
            await premium_service.fetch_premiums(
                db,
                "BTC",
                direction=PremiumDirection.FWD,
                exchanges=["coinbase"],
            )

    async def test_data_freshness_fields_are_filled(self, db) -> None:
        await seed_standard(db)
        res = await premium_service.fetch_premiums(
            db, "BTC", direction=PremiumDirection.FWD
        )
        assert res.data_oldest_at is not None
        assert res.data_newest_at is not None
        assert res.data_oldest_at <= res.data_newest_at


class TestServiceSignature:
    def test_direction_is_required(self) -> None:
        """방향은 기본값 없이 반드시 지정해야 한다 (엔드포인트가 결정)."""
        import inspect

        sig = inspect.signature(PremiumService.fetch_premiums)
        assert sig.parameters["direction"].default is inspect.Parameter.empty

    def test_values_are_url_friendly(self) -> None:
        assert PriceSide.BID.value == "bid"
        assert PriceSide.ASK.value == "ask"
        assert PremiumDirection.FWD.value == "fwd"
        assert PremiumDirection.REV.value == "rev"
