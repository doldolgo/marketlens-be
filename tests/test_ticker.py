"""티커(마지막 체결가) 파싱 테스트 (네트워크 불필요).

티커는 공개 엔드포인트가 아니라 수집기가 마지막 체결가를 저장할 때 쓰는
내부 조회 수단이다. 그래서 검증도 파싱 정확성에 집중한다.
"""

from __future__ import annotations

import pytest

from app.core.errors import ExchangeAPIError, MarketNotFoundError
from app.exchanges.connectors.binance import Binance
from app.exchanges.connectors.upbit import Upbit
from app.models.orderbook import MarketType
from app.models.symbol import Symbol
from app.models.ticker import Ticker

BTC_KRW = Symbol(base="BTC", quote="KRW")
BTC_USDT = Symbol(base="BTC", quote="USDT")


class TestUpbitTicker:
    def parse(self, raw):
        return Upbit()._parse_ticker(
            raw,
            symbol=BTC_KRW,
            native_symbol="KRW-BTC",
            market_type=MarketType.SPOT,
            latency_ms=5.0,
        )

    def test_parses_last_price_and_trade_time(self) -> None:
        ticker = self.parse(
            [
                {
                    "market": "KRW-BTC",
                    "trade_price": 90916000.0,
                    "trade_timestamp": 1786074475649,
                }
            ]
        )

        assert ticker.last_price == 90916000.0
        assert ticker.timestamp == 1786074475649
        assert ticker.native_symbol == "KRW-BTC"

    def test_period_summary_fields_are_ignored(self) -> None:
        """응답에 기간 요약이 와도 모델에 담지 않는다.

        업비트의 opening/high/low/change 는 00:00 UTC(=09:00 KST) 기준 당일 구간이라
        바이낸스의 롤링 24시간과 의미가 달라 섞으면 안 된다.
        """
        ticker = self.parse(
            [
                {
                    "trade_price": 100.0,
                    "trade_timestamp": 1,
                    "opening_price": 91248000.0,
                    "high_price": 91343000.0,
                    "low_price": 90700000.0,
                    "signed_change_rate": -0.0054905915,
                    "acc_trade_volume_24h": 491.88958314,
                    "acc_trade_price_24h": 44971270224.85124,
                }
            ]
        )

        assert ticker.last_price == 100.0
        for dropped in (
            "open_price",
            "high_price",
            "low_price",
            "change_percent",
            "volume_24h",
            "quote_volume_24h",
            "stats_window",
        ):
            assert not hasattr(ticker, dropped)

    def test_empty_response_raises(self) -> None:
        with pytest.raises(MarketNotFoundError):
            self.parse([])


class TestBinanceTicker:
    def parse(self, raw, market_type=MarketType.SPOT):
        return Binance()._parse_ticker(
            raw,
            symbol=BTC_USDT,
            native_symbol="BTCUSDT",
            market_type=market_type,
            latency_ms=5.0,
        )

    def test_parses_aggtrade_price_and_time(self) -> None:
        ticker = self.parse(
            [
                {
                    "a": 486,
                    "p": "231.95000000",
                    "q": "0.051",
                    "T": 1786073258565,
                    "m": False,
                }
            ]
        )

        assert ticker.last_price == 231.95
        assert isinstance(ticker.last_price, float)
        assert ticker.timestamp == 1786073258565  # 체결 시각 (윈도우 끝이 아님)

    def test_takes_latest_when_multiple_returned(self) -> None:
        """aggTrades 는 오래된 순으로 오므로 마지막 원소가 최신이다."""
        ticker = self.parse(
            [
                {"p": "100", "T": 1000},
                {"p": "200", "T": 2000},
                {"p": "300", "T": 3000},
            ]
        )
        assert ticker.last_price == 300.0
        assert ticker.timestamp == 3000

    def test_empty_trade_list_raises(self) -> None:
        with pytest.raises(MarketNotFoundError):
            self.parse([])

    def test_malformed_response_raises(self) -> None:
        with pytest.raises(ExchangeAPIError):
            self.parse([{"a": 1}])  # p / T 없음

    def test_futures_ticker_parses(self) -> None:
        ticker = self.parse(
            [{"p": "64265.70", "q": "0.002", "T": 1786077003799, "m": True}],
            market_type=MarketType.FUTURES,
        )
        assert ticker.market_type is MarketType.FUTURES
        assert ticker.last_price == 64265.70
        assert ticker.timestamp == 1786077003799

    def test_ticker_24hr_close_time_is_not_used(self) -> None:
        """closeTime 은 '윈도우 끝'이라 마지막 체결 시각이 아니다.

        실측: CRDOBUSDT 의 마지막 체결은 62분 전인데 closeTime 은 53초 전이었다.
        그래서 24hr 대신 aggTrades 를 쓴다 — 그 형태를 넘기면 파싱되면 안 된다.
        """
        with pytest.raises((ExchangeAPIError, MarketNotFoundError)):
            self.parse({"lastPrice": "100", "closeTime": 1786075472001})


class TestTickerModel:
    """모델이 마지막 체결가만 담는다는 계약을 고정한다."""

    def test_only_carries_last_price_and_metadata(self) -> None:
        assert set(Ticker.model_fields) == {
            "exchange",
            "symbol",
            "native_symbol",
            "market_type",
            "base",
            "quote",
            "last_price",
            "timestamp",
            "latency_ms",
        }

    def test_no_period_summary_fields(self) -> None:
        for dropped in (
            "open_price",
            "high_price",
            "low_price",
            "change_percent",
            "volume",
            "volume_24h",
            "quote_volume",
            "quote_volume_24h",
            "stats_window",
        ):
            assert dropped not in Ticker.model_fields


class TestTickerEndpointRouting:
    def test_upbit_uses_ticker_path(self) -> None:
        assert Upbit.TICKER_PATH == "/v1/ticker"

    def test_binance_uses_aggtrades_not_24hr_ticker(self) -> None:
        """24hr 티커의 closeTime 은 마지막 체결 시각이 아니라 윈도우 끝이다."""
        assert Binance.SPOT_TICKER_PATH == "/api/v3/aggTrades"
        assert Binance.FUTURES_TICKER_PATH == "/fapi/v1/aggTrades"
        assert "24hr" not in Binance.SPOT_TICKER_PATH
