"""가격 비교 로직 테스트 (네트워크 불필요)."""

from __future__ import annotations

import pytest

from app.core.errors import ExchangeAPIError
from app.models.orderbook import MarketType, OrderBook, OrderBookLevel
from app.services.comparison_service import ComparisonService

USDT_KRW = 1400.0


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


class TestConversion:
    def setup_method(self) -> None:
        self.service = ComparisonService()

    def test_same_currency_is_identity(self) -> None:
        assert self.service._conversion_factor("KRW", "KRW", USDT_KRW) == 1.0
        assert self.service._conversion_factor("USDT", "USDT", USDT_KRW) == 1.0

    def test_usdt_to_krw_multiplies_by_rate(self) -> None:
        assert self.service._conversion_factor("USDT", "KRW", USDT_KRW) == USDT_KRW

    def test_krw_to_usdt_divides_by_rate(self) -> None:
        assert self.service._conversion_factor("KRW", "USDT", USDT_KRW) == pytest.approx(
            1 / USDT_KRW
        )

    def test_round_trip_preserves_price(self) -> None:
        to_krw = self.service._conversion_factor("USDT", "KRW", USDT_KRW)
        back = self.service._conversion_factor("KRW", "USDT", USDT_KRW)
        assert 100.0 * to_krw * back == pytest.approx(100.0)

    def test_unsupported_currency_raises(self) -> None:
        with pytest.raises(ExchangeAPIError):
            self.service._conversion_factor("BTC", "KRW", USDT_KRW)

    def test_quote_is_converted_to_common_currency(self) -> None:
        book = make_book("binance", "USDT", bid=100.0, ask=101.0)
        quote = self.service._to_quote(book, USDT_KRW, "KRW")

        assert quote.best_bid == 100.0  # 원래 통화 값은 그대로 보존
        assert quote.best_bid_converted == 140_000.0
        assert quote.best_ask_converted == 141_400.0


class TestSpread:
    def setup_method(self) -> None:
        self.service = ComparisonService()

    def _quotes(self, books: list[OrderBook]):
        return [self.service._to_quote(b, USDT_KRW, "KRW") for b in books]

    def test_picks_cheapest_ask_and_highest_bid(self) -> None:
        quotes = self._quotes(
            [
                make_book("upbit", "KRW", bid=150_000.0, ask=151_000.0),
                make_book("binance", "USDT", bid=100.0, ask=101.0),  # -> 140,000 / 141,400
            ]
        )
        spread = self.service._build_spread(quotes)

        assert spread is not None
        assert spread.buy_exchange == "binance"
        assert spread.buy_price == 141_400.0
        assert spread.sell_exchange == "upbit"
        assert spread.sell_price == 150_000.0
        assert spread.absolute == pytest.approx(8_600.0)
        assert spread.percent == pytest.approx(8_600.0 / 141_400.0 * 100)

    def test_none_when_single_exchange(self) -> None:
        quotes = self._quotes([make_book("upbit", "KRW", bid=100.0, ask=101.0)])
        assert self.service._build_spread(quotes) is None

    def test_none_when_best_buy_and_sell_are_same_exchange(self) -> None:
        # 바이낸스가 최저 매도호가이면서 동시에 최고 매수호가인 경우 = 거래소 간 기회 없음
        quotes = self._quotes(
            [
                make_book("binance", "USDT", bid=100.0, ask=100.01),  # 140,000 / 140,014
                make_book("upbit", "KRW", bid=139_000.0, ask=145_000.0),
            ]
        )
        assert self.service._build_spread(quotes) is None


class TestTargetResolution:
    def test_uses_each_exchange_default_quote(self) -> None:
        targets = ComparisonService().resolve_targets("btc")
        resolved = {exchange_id: str(symbol) for exchange_id, symbol, _ in targets}

        assert resolved["upbit"] == "BTC/KRW"
        assert resolved["binance"] == "BTC/USDT"
