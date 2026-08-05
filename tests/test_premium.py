"""프리미엄 계산 로직 테스트 (네트워크 불필요)."""

from __future__ import annotations

import pytest

from app.core.errors import ExchangeAPIError, UnsupportedMarketError
from app.models.orderbook import MarketType, OrderBook, OrderBookLevel
from app.models.ticker import PriceBasis, Ticker
from app.services.fx import FxRate
from app.services.premium_service import PremiumService, PricePoint

FX = FxRate(rate=1400.0, source="test", timestamp=1700000000000)


def make_book(exchange: str, quote: str, bid: float, ask: float) -> OrderBook:
    return OrderBook(
        exchange=exchange,
        symbol=f"BTC/{quote}",
        native_symbol="X",
        market_type=MarketType.SPOT,
        base="BTC",
        quote=quote,
        bids=[OrderBookLevel(price=bid, size=1.0)],
        asks=[OrderBookLevel(price=ask, size=1.0)],
        timestamp=1700000000000,
        latency_ms=1.0,
    )


def make_ticker(exchange: str, quote: str, last: float) -> Ticker:
    return Ticker(
        exchange=exchange,
        symbol=f"BTC/{quote}",
        native_symbol="X",
        market_type=MarketType.SPOT,
        base="BTC",
        quote=quote,
        last_price=last,
        timestamp=1700000000000,
        latency_ms=1.0,
    )


def point(price: float, quote: str = "USDT") -> PricePoint:
    return PricePoint.from_source(make_ticker("binance", quote, price))


class TestPricePoint:
    """OrderBook 과 Ticker 를 같은 형태로 눕히는 어댑터."""

    def test_ticker_uses_last_price(self) -> None:
        p = PricePoint.from_source(make_ticker("binance", "USDT", 100.0))
        assert p.price == 100.0

    def test_orderbook_uses_mid_price(self) -> None:
        p = PricePoint.from_source(make_book("binance", "USDT", bid=90.0, ask=110.0))
        assert p.price == 100.0

    def test_metadata_is_preserved(self) -> None:
        p = PricePoint.from_source(make_ticker("binance", "USDT", 100.0))
        assert p.exchange == "binance"
        assert p.symbol == "BTC/USDT"
        assert p.quote == "USDT"
        assert p.timestamp == 1700000000000

    def test_empty_orderbook_raises(self) -> None:
        book = make_book("binance", "USDT", 100.0, 100.0)
        book.bids.clear()
        book.asks.clear()

        with pytest.raises(ExchangeAPIError):
            PricePoint.from_source(book)

    def test_zero_price_raises(self) -> None:
        with pytest.raises(ExchangeAPIError):
            PricePoint.from_source(make_ticker("binance", "USDT", 0.0))


class TestPremiumCalculation:
    def setup_method(self) -> None:
        self.service = PremiumService()

    def test_no_premium_when_prices_match_exactly(self) -> None:
        # 해외 100 USDT × 환율 1400 = 140,000원. 국내도 140,000원이면 프리미엄 0.
        entry = self.service._build_entry(point(100.0), krw_price=140_000.0, fx=FX)

        assert entry.premium_ratio == pytest.approx(1.0)
        assert entry.premium_percent == pytest.approx(0.0)
        assert entry.premium_krw == pytest.approx(0.0)

    def test_positive_premium_when_domestic_is_expensive(self) -> None:
        # 국내 147,000원 vs 해외 환산 140,000원 → 5% 김프
        entry = self.service._build_entry(point(100.0), krw_price=147_000.0, fx=FX)

        assert entry.premium_percent == pytest.approx(5.0)
        assert entry.premium_krw == pytest.approx(7_000.0)
        assert entry.price_in_krw == pytest.approx(140_000.0)

    def test_negative_premium_when_domestic_is_cheap(self) -> None:
        # 국내 133,000원 vs 해외 환산 140,000원 → -5% 역프
        entry = self.service._build_entry(point(100.0), krw_price=133_000.0, fx=FX)

        assert entry.premium_percent == pytest.approx(-5.0)
        assert entry.premium_krw == pytest.approx(-7_000.0)

    def test_ratio_is_independent_of_scale(self) -> None:
        """가격 단위가 커져도 비율은 같아야 한다 (BTC든 XRP든)."""
        cheap = self.service._build_entry(point(1.0), krw_price=1_470.0, fx=FX)
        pricey = self.service._build_entry(point(60_000.0), krw_price=88_200_000.0, fx=FX)

        assert cheap.premium_percent == pytest.approx(pricey.premium_percent)
        assert cheap.premium_percent == pytest.approx(5.0)

    def test_last_and_mid_agree_when_spread_is_symmetric(self) -> None:
        """중간가 100 인 호가와 체결가 100 인 티커는 같은 프리미엄을 낸다."""
        from_ticker = self.service._build_entry(
            PricePoint.from_source(make_ticker("binance", "USDT", 100.0)), 147_000.0, FX
        )
        from_book = self.service._build_entry(
            PricePoint.from_source(make_book("binance", "USDT", 90.0, 110.0)), 147_000.0, FX
        )
        assert from_ticker.premium_percent == pytest.approx(from_book.premium_percent)

    def test_basis_choice_changes_result_when_last_trade_hit_the_ask(self) -> None:
        """마지막 체결이 매도호가에서 났다면 체결가 기준이 프리미엄을 낮게 만든다."""
        # 호가 90/110 → 중간가 100. 마지막 체결은 매도호가 110 에서 발생.
        mid = self.service._build_entry(
            PricePoint.from_source(make_book("binance", "USDT", 90.0, 110.0)), 147_000.0, FX
        )
        last = self.service._build_entry(
            PricePoint.from_source(make_ticker("binance", "USDT", 110.0)), 147_000.0, FX
        )
        # 해외 가격이 더 비싸게 잡히므로 프리미엄은 더 작아진다.
        assert last.premium_percent < mid.premium_percent


class TestTargetResolution:
    def setup_method(self) -> None:
        self.service = PremiumService()

    def test_auto_selection_excludes_krw_reference_exchange(self) -> None:
        targets = self.service.resolve_targets("BTC", None, MarketType.SPOT)
        ids = [exchange_id for exchange_id, _, _ in targets]

        assert "upbit" not in ids          # 기준 거래소는 대상에서 제외
        assert "binance" in ids

    def test_auto_selection_uses_usdt_market(self) -> None:
        targets = self.service.resolve_targets("BTC", None, MarketType.SPOT)
        assert all(str(symbol) == "BTC/USDT" for _, symbol, _ in targets)

    def test_explicit_request_can_include_krw_reference_exchange(self) -> None:
        # 업비트를 명시하면 업비트 USDT 마켓과 비교한다 (거래소 내부 테더 괴리)
        targets = self.service.resolve_targets("BTC", ["upbit"], MarketType.SPOT)
        assert [exchange_id for exchange_id, _, _ in targets] == ["upbit"]

    def test_unsupported_market_raises(self) -> None:
        # 업비트는 선물 미지원
        with pytest.raises(UnsupportedMarketError):
            self.service.resolve_targets("BTC", ["upbit"], MarketType.FUTURES)


class TestPriceBasisEnum:
    def test_default_is_last_traded_price(self) -> None:
        import inspect

        sig = inspect.signature(PremiumService.fetch_premiums)
        assert sig.parameters["price_basis"].default is PriceBasis.LAST

    def test_values_are_url_friendly(self) -> None:
        assert PriceBasis.LAST.value == "last"
        assert PriceBasis.MID.value == "mid"
