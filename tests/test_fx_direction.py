"""방향별·거래소별 환율 테스트 — 작업지시서 §6 완료 판정 기준.

환율을 은행 고시 단일 값에서 **국내 거래소 KRW-USDT 방향별 호가**로 바꾼
변경의 계약을 못박는다. 세 가지를 본다.

    1. 회귀 성질 — ask == bid == R 이면 결과가 단일 환율 R 일 때와 완전히 같다.
       (이게 깨지면 공식 적용이 틀린 것이다)
    2. 방향 분리 — ask != bid 면 fwd 는 ask, rev 는 bid 를 쓴다.
    3. 거래소 분리 — 같은 코인이라도 dom 이 다르면 다른 환율을 쓴다.
"""

from __future__ import annotations

import pytest
from conftest import (
    BINANCE_PRICES,
    BITHUMB_PRICES,
    FX_RATE,
    UPBIT_PRICES,
    best_ask,
    best_bid,
    fwd_execution_percent,
    rev_execution_percent,
    seed_rows,
    seed_usdkrw_rate,
    snapshot_row,
)

from app.models.premium import PremiumDirection
from app.services.arbitrage_service import arbitrage_service
from app.services.matrix_service import matrix_service
from app.services.premium_service import premium_service
from app.services.spread_service import spread_service

#: 테더 프리미엄이 벌어진 상황 — 업비트와 빗썸이 서로 다른 값을 보인다.
UPBIT_ASK, UPBIT_BID = 1400.0, 1390.0
BITHUMB_ASK, BITHUMB_BID = 1420.0, 1410.0


async def seed_prices(db) -> None:
    """환율만 빼고 표준 시나리오와 같은 시세를 심는다."""
    await seed_rows(
        db, "upbit", [snapshot_row("upbit", b, p) for b, p in UPBIT_PRICES.items()]
    )
    await seed_rows(
        db,
        "bithumb",
        [snapshot_row("bithumb", b, p) for b, p in BITHUMB_PRICES.items()],
    )
    await seed_rows(
        db,
        "binance",
        [
            snapshot_row("binance", b, p, quote="USDT", krw_factor=FX_RATE)
            for b, p in BINANCE_PRICES.items()
        ],
    )


async def seed_split(db) -> None:
    """방향·거래소가 전부 다른 환율을 심는다."""
    await seed_prices(db)
    await seed_usdkrw_rate(db, ask=UPBIT_ASK, bid=UPBIT_BID, exchanges=("upbit",))
    await seed_usdkrw_rate(
        db, ask=BITHUMB_ASK, bid=BITHUMB_BID, exchanges=("bithumb",)
    )


def row_of(rows, sym: str, dom: str):
    return next(r for r in rows if r.sym == sym and r.dom == dom)


class TestRegression:
    """ask == bid == R 이면 단일 환율 R 일 때와 결과가 같아야 한다."""

    async def test_spreads_match_single_rate_formula(self, db) -> None:
        await seed_prices(db)
        await seed_usdkrw_rate(db, FX_RATE)  # ask == bid == 1400

        res = await spread_service.build(db)
        row = row_of(res.rows, "BTC", "upbit")

        assert row.fwd == pytest.approx(
            fwd_execution_percent(UPBIT_PRICES["BTC"], BINANCE_PRICES["BTC"], FX_RATE)
        )
        assert row.rev == pytest.approx(
            rev_execution_percent(UPBIT_PRICES["BTC"], BINANCE_PRICES["BTC"], FX_RATE)
        )
        assert row.rate_ask == row.rate_bid == FX_RATE

    async def test_premium_matches_single_rate_formula(self, db) -> None:
        await seed_prices(db)
        await seed_usdkrw_rate(db, FX_RATE)

        for direction, expected in (
            (PremiumDirection.FWD, fwd_execution_percent),
            (PremiumDirection.REV, rev_execution_percent),
        ):
            res = await premium_service.fetch_premiums(
                db, "BTC", direction=direction, domestic="upbit"
            )
            assert res.usd_krw_rate == FX_RATE
            assert res.premiums[0].premium_percent == pytest.approx(
                expected(UPBIT_PRICES["BTC"], BINANCE_PRICES["BTC"], FX_RATE)
            )

    async def test_arbitrage_matches_single_rate_formula(self, db) -> None:
        """스프레드가 0 이면 매수·매도측 환율이 갈려도 값이 같다."""
        await seed_prices(db)
        await seed_usdkrw_rate(db, FX_RATE)

        res = await arbitrage_service.simulate(
            db, "BTC", amount=1_000_000.0, direction=PremiumDirection.FWD
        )
        # 해외에서 사서 국내에 판다 — 두 다리 모두 환율 1400 으로 환산된다.
        assert res.usd_krw_rate == FX_RATE
        assert res.buy.exchange == "binance"
        assert res.buy.average_price_krw == pytest.approx(
            best_ask(BINANCE_PRICES["BTC"]) * FX_RATE
        )


class TestDirectionSplit:
    """ask != bid 면 fwd 와 rev 가 서로 다른 환율을 쓴다."""

    async def test_spreads_use_ask_for_fwd_and_bid_for_rev(self, db) -> None:
        await seed_split(db)
        res = await spread_service.build(db)
        row = row_of(res.rows, "BTC", "upbit")

        assert (row.rate_ask, row.rate_bid) == (UPBIT_ASK, UPBIT_BID)
        assert row.fwd == pytest.approx(
            fwd_execution_percent(
                UPBIT_PRICES["BTC"], BINANCE_PRICES["BTC"], UPBIT_ASK
            )
        )
        assert row.rev == pytest.approx(
            rev_execution_percent(
                UPBIT_PRICES["BTC"], BINANCE_PRICES["BTC"], UPBIT_BID
            )
        )
        # 같은 환율을 썼다면 실패 — 두 방향이 같은 값을 쓰면 안 된다.
        assert row.fwd != pytest.approx(
            fwd_execution_percent(
                UPBIT_PRICES["BTC"], BINANCE_PRICES["BTC"], UPBIT_BID
            )
        )

    async def test_spread_makes_both_directions_more_conservative(self, db) -> None:
        """ask > bid 이므로 양방향 모두 스프레드 0 일 때보다 낮게 나온다."""
        await seed_prices(db)
        await seed_usdkrw_rate(db, ask=UPBIT_ASK, bid=UPBIT_ASK, exchanges=("upbit",))
        tight = row_of((await spread_service.build(db)).rows, "BTC", "upbit")

        await seed_usdkrw_rate(db, ask=UPBIT_ASK, bid=UPBIT_BID, exchanges=("upbit",))
        wide = row_of((await spread_service.build(db)).rows, "BTC", "upbit")

        assert wide.fwd == pytest.approx(tight.fwd)  # 김프는 ask 만 쓰므로 그대로
        assert wide.rev < tight.rev  # 역프는 더 싸게 팔게 되므로 나빠진다

    async def test_premium_picks_the_matching_side(self, db) -> None:
        await seed_split(db)

        fwd = await premium_service.fetch_premiums(
            db, "BTC", direction=PremiumDirection.FWD, domestic="upbit"
        )
        rev = await premium_service.fetch_premiums(
            db, "BTC", direction=PremiumDirection.REV, domestic="upbit"
        )

        assert fwd.usd_krw_rate == UPBIT_ASK
        assert rev.usd_krw_rate == UPBIT_BID

    async def test_arbitrage_applies_the_rate_of_each_leg(self, db) -> None:
        """차익 시뮬레이션은 매수 다리와 매도 다리에 각각 맞는 환율을 쓴다.

        김프는 해외에서 **사므로** 원화로 USDT 를 사고(ask), 역프는 해외에
        **팔아** 받은 USDT 를 원화로 판다(bid).
        """
        await seed_split(db)

        fwd = await arbitrage_service.simulate(
            db, "BTC", amount=1_000_000.0, direction=PremiumDirection.FWD
        )
        assert fwd.buy.exchange == "binance"
        assert fwd.buy.average_price_krw == pytest.approx(
            best_ask(BINANCE_PRICES["BTC"]) * UPBIT_ASK
        )

        rev = await arbitrage_service.simulate(
            db, "BTC", amount=1_000_000.0, direction=PremiumDirection.REV
        )
        assert rev.sell.exchange == "binance"
        assert rev.sell.average_price_krw == pytest.approx(
            best_bid(BINANCE_PRICES["BTC"]) * UPBIT_BID
        )

    async def test_matrix_uses_ask_to_buy_and_bid_to_sell_overseas(self, db) -> None:
        """매트릭스도 해외 매수는 ask, 해외 매도는 bid 로 환산한다."""
        await seed_split(db)
        res = await matrix_service.build(db, amount_krw=1_000_000.0)
        btc = next(e for e in res.coins if e.sym == "BTC")

        # 김프: 해외에서 산다 → 해외 매도호가를 그 국내 거래소의 ask 로 환산.
        # 국내 거래소마다 환율이 달라 조합별 값이 갈리고, 매트릭스는 최댓값을 고른다.
        assert btc.fwd is not None
        assert btc.fwd.premium_percent == pytest.approx(
            max(
                fwd_execution_percent(
                    UPBIT_PRICES["BTC"], BINANCE_PRICES["BTC"], UPBIT_ASK
                ),
                fwd_execution_percent(
                    BITHUMB_PRICES["BTC"], BINANCE_PRICES["BTC"], BITHUMB_ASK
                ),
            )
        )
        # 역프: 해외에 판다 → 해외 매수호가를 bid 로 환산.
        assert btc.rev is not None
        assert btc.rev.premium_percent == pytest.approx(
            max(
                rev_execution_percent(
                    UPBIT_PRICES["BTC"], BINANCE_PRICES["BTC"], UPBIT_BID
                ),
                rev_execution_percent(
                    BITHUMB_PRICES["BTC"], BINANCE_PRICES["BTC"], BITHUMB_BID
                ),
            )
        )


class TestExchangeSplit:
    """같은 코인이라도 dom 이 다르면 다른 환율을 쓴다."""

    async def test_spread_rows_carry_their_own_exchange_rate(self, db) -> None:
        await seed_split(db)
        rows = (await spread_service.build(db)).rows

        upbit_row = row_of(rows, "BTC", "upbit")
        bithumb_row = row_of(rows, "BTC", "bithumb")

        assert (upbit_row.rate_ask, upbit_row.rate_bid) == (UPBIT_ASK, UPBIT_BID)
        assert (bithumb_row.rate_ask, bithumb_row.rate_bid) == (
            BITHUMB_ASK,
            BITHUMB_BID,
        )
        assert bithumb_row.fwd == pytest.approx(
            fwd_execution_percent(
                BITHUMB_PRICES["BTC"], BINANCE_PRICES["BTC"], BITHUMB_ASK
            )
        )

    async def test_domestic_without_rate_is_dropped(self, db) -> None:
        """USDT 호가가 한 번도 없던 국내 거래소는 남의 환율을 빌리지 않는다."""
        await seed_prices(db)
        await seed_usdkrw_rate(db, ask=UPBIT_ASK, bid=UPBIT_BID, exchanges=("upbit",))

        rows = (await spread_service.build(db)).rows
        assert {r.dom for r in rows} == {"upbit"}

    async def test_premium_follows_the_requested_domestic_exchange(self, db) -> None:
        await seed_split(db)

        upbit = await premium_service.fetch_premiums(
            db, "BTC", direction=PremiumDirection.FWD, domestic="upbit"
        )
        bithumb = await premium_service.fetch_premiums(
            db, "BTC", direction=PremiumDirection.FWD, domestic="bithumb"
        )

        assert upbit.usd_krw_rate == UPBIT_ASK
        assert bithumb.usd_krw_rate == BITHUMB_ASK
