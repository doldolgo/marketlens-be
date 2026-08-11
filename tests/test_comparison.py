"""거래소 간 가격 비교 테스트 — DB 스냅샷 기반 (네트워크 불필요)."""

from __future__ import annotations

import pytest
from conftest import (
    BINANCE_PRICES,
    BITHUMB_PRICES,
    KRW_RATES,
    NOW_MS,
    UPBIT_PRICES,
    best_ask,
    best_bid,
    seed_rows,
    seed_standard,
    snapshot_row,
)

from app.core.errors import (
    InvalidRequestError,
    MarketDataNotFoundError,
    UnsupportedExchangeError,
)
from app.db.models import KrwRate, MarketSnapshot
from app.services.comparison_service import ComparisonService, comparison_service

UPBIT_RATE = KrwRate(exchange="upbit", rate=1400.0)
BITHUMB_RATE = KrwRate(exchange="bithumb", rate=1401.0)
RATES = {"upbit": UPBIT_RATE, "bithumb": BITHUMB_RATE}


def make_snap(
    exchange: str,
    quote: str,
    *,
    price: float = 100.0,
    bid: float | None = None,
    ask: float | None = None,
) -> MarketSnapshot:
    return MarketSnapshot(
        exchange=exchange,
        base="BTC",
        native_symbol="X",
        quote=quote,
        price=price,
        bids=[[bid, 1.0]] if bid is not None else [],
        asks=[[ask, 1.0]] if ask is not None else [],
        price_timestamp=NOW_MS,
    )


class TestConversion:
    def setup_method(self) -> None:
        self.service = ComparisonService()

    def test_same_currency_is_identity(self) -> None:
        assert self.service._conversion(
            make_snap("upbit", "KRW"), "KRW", RATES, UPBIT_RATE
        ) == (1.0, None)
        assert self.service._conversion(
            make_snap("binance", "USDT"), "USDT", RATES, UPBIT_RATE
        ) == (1.0, None)

    def test_usdt_to_krw_multiplies_by_reference_rate(self) -> None:
        factor, applied = self.service._conversion(
            make_snap("binance", "USDT"), "KRW", RATES, UPBIT_RATE
        )
        assert factor == 1400.0
        assert applied == 1400.0

    def test_krw_to_usdt_divides_by_own_rate(self) -> None:
        """국내 거래소는 자기 KRW-USDT 환율로 나눈다 (빗썸이면 1401)."""
        factor, applied = self.service._conversion(
            make_snap("bithumb", "KRW"), "USDT", RATES, UPBIT_RATE
        )
        assert factor == pytest.approx(1 / 1401.0)
        assert applied == 1401.0

    def test_krw_to_usdt_falls_back_to_reference_rate(self) -> None:
        factor, applied = self.service._conversion(
            make_snap("bithumb", "KRW"), "USDT", {"upbit": UPBIT_RATE}, UPBIT_RATE
        )
        assert factor == pytest.approx(1 / 1400.0)
        assert applied == 1400.0

    def test_missing_rate_raises(self) -> None:
        with pytest.raises(MarketDataNotFoundError):
            self.service._conversion(make_snap("binance", "USDT"), "KRW", {}, None)

    def test_round_trip_preserves_price(self) -> None:
        to_krw, _ = self.service._conversion(
            make_snap("binance", "USDT"), "KRW", RATES, UPBIT_RATE
        )
        back, _ = self.service._conversion(
            make_snap("upbit", "KRW"), "USDT", RATES, UPBIT_RATE
        )
        assert 100.0 * to_krw * back == pytest.approx(100.0)

    def test_to_quote_converts_prices(self) -> None:
        snap = make_snap("binance", "USDT", price=100.5, bid=100.0, ask=101.0)
        quote = self.service._to_quote(snap, 1400.0)

        # price/best_bid/best_ask 는 공통 통화로 환산된 값이다
        assert quote.price == pytest.approx(140_700.0)
        assert quote.best_bid == pytest.approx(140_000.0)
        assert quote.best_ask == pytest.approx(141_400.0)
        assert quote.quote_currency == "USDT"  # 원래 통화는 따로 남는다

    def test_to_quote_handles_empty_book(self) -> None:
        quote = self.service._to_quote(make_snap("upbit", "KRW"), 1.0)
        assert quote.best_bid is None and quote.best_ask is None


class TestSpread:
    def setup_method(self) -> None:
        self.service = ComparisonService()

    def _quotes(self, specs):
        """specs: (exchange, quote, bid, ask, factor)"""
        return [
            self.service._to_quote(make_snap(e, q, bid=b, ask=a), f)
            for e, q, b, a, f in specs
        ]

    def test_picks_cheapest_ask_and_highest_bid(self) -> None:
        quotes = self._quotes(
            [
                ("upbit", "KRW", 150_000.0, 151_000.0, 1.0),
                ("binance", "USDT", 100.0, 101.0, 1400.0),  # → 140,000 / 141,400
            ]
        )
        spread = self.service._build_spread(quotes)

        assert spread is not None
        assert spread.buy_exchange == "binance"
        assert spread.buy_price == pytest.approx(141_400.0)
        assert spread.sell_exchange == "upbit"
        assert spread.sell_price == pytest.approx(150_000.0)
        assert spread.absolute == pytest.approx(8_600.0)
        assert spread.percent == pytest.approx(8_600.0 / 141_400.0 * 100)

    def test_none_when_single_exchange(self) -> None:
        quotes = self._quotes([("upbit", "KRW", 100.0, 101.0, 1.0)])
        assert self.service._build_spread(quotes) is None

    def test_none_when_best_buy_and_sell_are_same_exchange(self) -> None:
        # 바이낸스가 최저 매도호가이면서 동시에 최고 매수호가 = 거래소 간 기회 없음
        quotes = self._quotes(
            [
                ("binance", "USDT", 100.0, 100.01, 1400.0),  # 140,000 / 140,014
                ("upbit", "KRW", 139_000.0, 145_000.0, 1.0),
            ]
        )
        assert self.service._build_spread(quotes) is None


class TestCompare:
    async def test_invalid_currency_raises(self, db) -> None:
        with pytest.raises(InvalidRequestError):
            await comparison_service.compare(db, "BTC", common_currency="EUR")

    async def test_empty_db_raises(self, db) -> None:
        with pytest.raises(MarketDataNotFoundError):
            await comparison_service.compare(db, "BTC")

    async def test_krw_comparison(self, db) -> None:
        await seed_standard(db)
        res = await comparison_service.compare(db, "btc")

        assert res.sym == "BTC"
        assert res.common_currency == "KRW"
        assert res.usdt_krw_rate == KRW_RATES["upbit"]  # 기준 거래소 환율
        assert res.rate_exchange == "upbit"

        # 환산가 오름차순: 바이낸스(99.4M) < 업비트(100M) < 빗썸(100.1M)
        assert [q.exchange for q in res.quotes] == ["binance", "upbit", "bithumb"]
        binance = res.quotes[0]
        assert binance.price == pytest.approx(
            BINANCE_PRICES["BTC"] * KRW_RATES["upbit"]
        )
        # KRW 행은 환산이 없다 — 원래 가격 그대로
        assert res.quotes[1].price == pytest.approx(UPBIT_PRICES["BTC"])

    async def test_krw_spread_buys_binance_sells_bithumb(self, db) -> None:
        await seed_standard(db)
        res = await comparison_service.compare(db, "BTC")

        spread = res.spread
        assert spread is not None
        assert spread.buy_exchange == "binance"
        assert spread.sell_exchange == "bithumb"
        expected_buy = best_ask(BINANCE_PRICES["BTC"]) * KRW_RATES["upbit"]
        expected_sell = best_bid(BITHUMB_PRICES["BTC"])
        assert spread.buy_price == pytest.approx(expected_buy)
        assert spread.sell_price == pytest.approx(expected_sell)
        assert spread.percent == pytest.approx(
            (expected_sell - expected_buy) / expected_buy * 100
        )

    async def test_usdt_comparison_uses_each_domestic_own_rate(self, db) -> None:
        await seed_standard(db)
        res = await comparison_service.compare(db, "BTC", common_currency="USDT")

        by_exchange = {q.exchange: q for q in res.quotes}
        assert by_exchange["upbit"].price == pytest.approx(
            UPBIT_PRICES["BTC"] / KRW_RATES["upbit"]
        )
        assert by_exchange["bithumb"].price == pytest.approx(
            BITHUMB_PRICES["BTC"] / KRW_RATES["bithumb"]
        )
        assert by_exchange["binance"].price == BINANCE_PRICES["BTC"]
        assert [q.exchange for q in res.quotes][0] == "binance"  # 가장 싸다

    async def test_exchange_filter_and_missing_exchanges(self, db) -> None:
        await seed_standard(db)
        # 빗썸에는 ETH 가 없다
        res = await comparison_service.compare(
            db, "ETH", exchanges=["upbit", "bithumb", "binance"]
        )

        assert {q.exchange for q in res.quotes} == {"upbit", "binance"}
        assert res.missing_exchanges == ["bithumb"]

    async def test_single_exchange_has_no_spread(self, db) -> None:
        await seed_standard(db)
        res = await comparison_service.compare(db, "BTC", exchanges=["upbit"])

        assert len(res.quotes) == 1
        assert res.spread is None

    async def test_unknown_exchange_raises(self, db) -> None:
        await seed_standard(db)
        with pytest.raises(UnsupportedExchangeError):
            await comparison_service.compare(db, "BTC", exchanges=["coinbase"])

    async def test_krw_only_snapshots_work_without_rates(self, db) -> None:
        """KRW 행만 비교하면 환율이 없어도 동작한다 (환산이 필요 없다)."""
        await seed_rows(db, "upbit", [snapshot_row("upbit", "BTC", 100_000_000.0)])
        res = await comparison_service.compare(db, "BTC")

        assert res.usdt_krw_rate is None
        assert res.rate_exchange is None
        assert len(res.quotes) == 1
