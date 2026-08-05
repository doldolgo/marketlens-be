"""거래소 커넥터의 심볼 변환 / 응답 파싱 테스트 (네트워크 불필요)."""

from __future__ import annotations

import pytest

from app.core.errors import InvalidSymbolError, MarketNotFoundError, UnsupportedMarketError
from app.exchanges import all_exchanges
from app.exchanges.connectors.binance import Binance
from app.exchanges.connectors.upbit import Upbit
from app.models.orderbook import MarketType
from app.models.symbol import Symbol

BTC_KRW = Symbol(base="BTC", quote="KRW")
BTC_USDT = Symbol(base="BTC", quote="USDT")


class TestSymbol:
    @pytest.mark.parametrize("raw", ["BTC/KRW", "btc/krw", "BTC-KRW", "btc_krw", " BTC/KRW "])
    def test_parse_accepts_common_formats(self, raw: str) -> None:
        assert Symbol.parse(raw) == BTC_KRW

    @pytest.mark.parametrize("raw", ["BTC", "", "BTC/KRW/USDT", "/"])
    def test_parse_rejects_malformed(self, raw: str) -> None:
        with pytest.raises(InvalidSymbolError):
            Symbol.parse(raw)


class TestUpbit:
    def test_native_symbol_is_quote_first(self) -> None:
        assert Upbit().to_native_symbol(BTC_KRW, MarketType.SPOT) == "KRW-BTC"

    def test_parse_orderbook(self) -> None:
        raw = [
            {
                "market": "KRW-BTC",
                "timestamp": 1700000000000,
                "orderbook_units": [
                    {"ask_price": 101.0, "ask_size": 1.0, "bid_price": 99.0, "bid_size": 2.0},
                    {"ask_price": 102.0, "ask_size": 3.0, "bid_price": 98.0, "bid_size": 4.0},
                ],
            }
        ]
        book = Upbit()._parse_orderbook(
            raw,
            symbol=BTC_KRW,
            native_symbol="KRW-BTC",
            market_type=MarketType.SPOT,
            depth=2,
            latency_ms=12.3,
        )

        assert book.best_bid == 99.0
        assert book.best_ask == 101.0
        assert book.mid_price == 100.0
        assert book.spread == 2.0
        assert book.timestamp == 1700000000000

    def test_depth_truncates_response(self) -> None:
        raw = [
            {
                "market": "KRW-BTC",
                "timestamp": 1700000000000,
                "orderbook_units": [
                    {"ask_price": 100.0 + i, "ask_size": 1.0, "bid_price": 99.0 - i, "bid_size": 1.0}
                    for i in range(30)
                ],
            }
        ]
        book = Upbit()._parse_orderbook(
            raw,
            symbol=BTC_KRW,
            native_symbol="KRW-BTC",
            market_type=MarketType.SPOT,
            depth=5,
            latency_ms=1.0,
        )
        assert len(book.bids) == 5
        assert len(book.asks) == 5

    def test_empty_response_raises(self) -> None:
        with pytest.raises(MarketNotFoundError):
            Upbit()._parse_orderbook(
                [],
                symbol=BTC_KRW,
                native_symbol="KRW-NOPE",
                market_type=MarketType.SPOT,
                depth=5,
                latency_ms=1.0,
            )

    def test_rejects_futures(self) -> None:
        with pytest.raises(UnsupportedMarketError):
            Upbit().ensure_supported(BTC_KRW, MarketType.FUTURES)


class TestBinance:
    def test_native_symbol_has_no_separator(self) -> None:
        assert Binance().to_native_symbol(BTC_USDT, MarketType.SPOT) == "BTCUSDT"

    @pytest.mark.parametrize(
        ("requested", "expected"), [(1, 5), (5, 5), (7, 10), (10, 10), (30, 50), (9999, 1000)]
    )
    def test_limit_is_rounded_up_to_allowed_value(self, requested: int, expected: int) -> None:
        assert Binance()._normalize_limit(requested) == expected

    def test_parse_orderbook_converts_string_prices(self) -> None:
        raw = {
            "lastUpdateId": 1,
            "bids": [["99.00000000", "2.00000000"], ["98.00000000", "4.00000000"]],
            "asks": [["101.00000000", "1.00000000"], ["102.00000000", "3.00000000"]],
        }
        book = Binance()._parse_orderbook(
            raw,
            symbol=BTC_USDT,
            native_symbol="BTCUSDT",
            market_type=MarketType.SPOT,
            depth=2,
            latency_ms=9.9,
        )

        assert book.best_bid == 99.0
        assert book.best_ask == 101.0
        assert isinstance(book.bids[0].price, float)

    def test_futures_uses_event_timestamp(self) -> None:
        raw = {"E": 1700000000000, "T": 1699999999999, "bids": [["1", "1"]], "asks": [["2", "1"]]}
        book = Binance()._parse_orderbook(
            raw,
            symbol=BTC_USDT,
            native_symbol="BTCUSDT",
            market_type=MarketType.FUTURES,
            depth=1,
            latency_ms=1.0,
        )
        assert book.timestamp == 1700000000000


class TestConnectorContract:
    """모든 커넥터가 지켜야 하는 규약. 새 거래소를 추가해도 자동으로 검증된다."""

    def test_every_connector_declares_required_metadata(self) -> None:
        for exchange in all_exchanges():
            assert exchange.id and exchange.id.islower()
            assert exchange.name
            assert exchange.quote_currencies
            assert exchange.default_quote in exchange.quote_currencies
            assert exchange.supported_market_types

    def test_native_symbol_contains_base_and_quote(self) -> None:
        for exchange in all_exchanges():
            symbol = Symbol(base="BTC", quote=exchange.default_quote)
            native = exchange.to_native_symbol(symbol, MarketType.SPOT)
            assert "BTC" in native
            assert exchange.default_quote in native
