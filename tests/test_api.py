"""엔드포인트 통합 검증 — 실제 앱 + in-memory SQLite 세션 주입.

거래소 호출은 전혀 없다. 조회 API 는 DB 만 읽으므로, 표준 시나리오를 심어두고
라우팅 · 파라미터 검증 · 서비스 계산 · pydantic 직렬화 · 에러 핸들러까지
전부 실제 코드로 태운다 (httpx.ASGITransport, lifespan 미실행).
"""

from __future__ import annotations

import pytest
from conftest import (
    BINANCE_PRICES,
    FX_RATE,
    NOW_MS,
    UPBIT_PRICES,
)


class TestBasicEndpoints:
    async def test_health(self, client) -> None:
        r = await client.get("/health")
        assert r.status_code == 200 and r.json()["status"] == "ok"

    async def test_exchanges_lists_all_three(self, client) -> None:
        r = await client.get("/exchanges")
        ids = {e["id"] for e in r.json()}
        assert ids == {"upbit", "bithumb", "binance"}

    async def test_openapi_documents_all_endpoints(self, client) -> None:
        paths = (await client.get("/openapi.json")).json()["paths"]

        assert set(paths) >= {
            "/health",
            "/refresh",
            "/exchanges",
            "/rate",
            "/orderbook/{exchange_id}",
            "/compare",
            "/premium",
            "/premium/fwd",
            "/premium/rev",
            "/premium/scan",
            "/slippage/{exchange_id}",
            "/matrix",
            "/arbitrage",
            "/history/premium",
            "/history/status",
        }

    async def test_every_operation_has_summary(self, client) -> None:
        paths = (await client.get("/openapi.json")).json()["paths"]
        for path, methods in paths.items():
            for method, op in methods.items():
                assert op.get("summary"), f"{method.upper()} {path} 에 summary 없음"


class TestFxEndpoint:
    async def test_empty_db_is_404(self, client) -> None:
        r = await client.get("/rate")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "market_data_not_found"

    async def test_returns_rate_per_exchange(self, seeded_client) -> None:
        """환율은 국내 거래소별 KRW-USDT 호가 (ask/bid) 다."""
        d = (await seeded_client.get("/rate")).json()

        assert [r["exchange"] for r in d["rates"]] == ["bithumb", "upbit"]
        for row in d["rates"]:
            assert row["ask"] == FX_RATE
            assert row["bid"] == FX_RATE
            assert row["updated_at"] is not None


class TestOrderbookEndpoint:
    async def test_returns_stored_book(self, seeded_client) -> None:
        d = (
            await seeded_client.get("/orderbook/upbit?symbol=BTC/KRW&depth=3")
        ).json()

        assert d["exchange"] == "upbit"
        assert d["symbol"] == "BTC/KRW"
        assert "native_symbol" not in d  # 원본 심볼은 응답에서 제외한다
        assert d["quote"] == "KRW"
        assert d["timestamp"] == NOW_MS
        assert len(d["bids"]) == 3 and len(d["asks"]) == 3  # depth 로 잘린다
        assert d["bids"][0]["price"] > d["bids"][1]["price"]  # 내림차순
        assert d["asks"][0]["price"] < d["asks"][1]["price"]  # 오름차순
        assert d["asks"][0]["price"] == pytest.approx(
            UPBIT_PRICES["BTC"] * 1.0005
        )

    async def test_binance_usdt_market(self, seeded_client) -> None:
        d = (await seeded_client.get("/orderbook/binance?symbol=BTC/USDT")).json()
        assert d["symbol"] == "BTC/USDT"
        assert d["quote"] == "USDT"

    async def test_wrong_quote_is_404_with_hint(self, seeded_client) -> None:
        r = await seeded_client.get("/orderbook/upbit?symbol=BTC/USDT")
        assert r.status_code == 404
        detail = r.json()["error"]["detail"]
        assert detail["stored_quote"] == "KRW"

    async def test_empty_db_is_404(self, client) -> None:
        r = await client.get("/orderbook/upbit?symbol=BTC/KRW")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "market_data_not_found"

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("/orderbook/coinbase?symbol=BTC/KRW", 404),  # 없는 거래소
            ("/orderbook/upbit?symbol=BTC", 400),  # 심볼 형식
            ("/orderbook/upbit?symbol=BTC/KRW&depth=999", 422),  # le=100
        ],
    )
    async def test_error_status_codes(self, seeded_client, url, expected) -> None:
        assert (await seeded_client.get(url)).status_code == expected


class TestCompareEndpoint:
    async def test_krw_comparison(self, seeded_client) -> None:
        d = (await seeded_client.get("/compare?sym=BTC")).json()

        assert d["common_currency"] == "KRW"
        assert [q["exchange"] for q in d["quotes"]] == [
            "binance",
            "upbit",
            "bithumb",
        ]  # 환산가 오름차순
        assert d["usd_krw_rate"] == FX_RATE
        assert d["spread"]["buy_exchange"] == "binance"
        assert d["spread"]["sell_exchange"] == "bithumb"
        assert d["spread"]["percent"] > 0

    async def test_usdt_comparison(self, seeded_client) -> None:
        d = (await seeded_client.get("/compare?sym=BTC&common_currency=USDT")).json()

        binance = next(q for q in d["quotes"] if q["exchange"] == "binance")
        assert binance["price"] == BINANCE_PRICES["BTC"]

    async def test_missing_exchange_is_reported(self, seeded_client) -> None:
        d = (
            await seeded_client.get(
                "/compare?sym=ETH&exchanges=upbit&exchanges=bithumb&exchanges=binance"
            )
        ).json()
        assert d["missing_exchanges"] == ["bithumb"]

    async def test_invalid_currency_is_400(self, seeded_client) -> None:
        r = await seeded_client.get("/compare?sym=BTC&common_currency=EUR")
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "invalid_request"

    async def test_empty_db_is_404(self, client) -> None:
        assert (await client.get("/compare?sym=BTC")).status_code == 404


class TestPremiumEndpoints:
    async def test_kimchi_and_reverse_have_opposite_signs(self, seeded_client) -> None:
        k = (await seeded_client.get("/premium/fwd?sym=BTC")).json()
        r = (await seeded_client.get("/premium/rev?sym=BTC")).json()

        kp = k["premiums"][0]["premium_percent"]
        rp = r["premiums"][0]["premium_percent"]
        assert kp > 0 and rp < 0  # 국내가 비싼 시나리오

    async def test_search_returns_both_directions(self, seeded_client) -> None:
        d = (await seeded_client.get("/premium?sym=BTC")).json()

        assert d["fwd"]["direction"] == "fwd"
        assert d["rev"]["direction"] == "rev"
        assert d["best_direction"] == "fwd"
        assert d["best_premium_percent"] > 0
        assert d["fwd"]["dom"] == "upbit"

    async def test_uses_execution_sides(self, seeded_client) -> None:
        """김프는 국내 bid · 해외 ask, 역김프는 그 반대를 쓴다."""
        k = (await seeded_client.get("/premium/fwd?sym=BTC")).json()
        r = (await seeded_client.get("/premium/rev?sym=BTC")).json()

        # 방향마다 집는 호가가 달라 국내가/해외 원화가가 서로 다르다
        assert k["dom_price"] < r["dom_price"]  # bid < ask
        assert k["premiums"][0]["usd"] > r["premiums"][0]["usd"]

    async def test_domestic_selection_changes_result(self, seeded_client) -> None:
        upbit = (
            await seeded_client.get("/premium/fwd?sym=BTC&dom=upbit")
        ).json()
        bithumb = (
            await seeded_client.get("/premium/fwd?sym=BTC&dom=bithumb")
        ).json()

        assert upbit["dom_price"] != bithumb["dom_price"]
        # 환율은 통일 환율 하나 — 국내 거래소를 바꿔도 같다
        assert upbit["usd_krw_rate"] == bithumb["usd_krw_rate"] == 1400.0

    async def test_overseas_domestic_is_400(self, seeded_client) -> None:
        r = await seeded_client.get("/premium?sym=BTC&dom=binance")
        assert r.status_code == 400

    async def test_coin_not_listed_domestically_is_404(self, seeded_client) -> None:
        r = await seeded_client.get("/premium/fwd?sym=SOL")  # 업비트 미상장
        assert r.status_code == 404

    async def test_unknown_overseas_exchange_is_404(self, seeded_client) -> None:
        r = await seeded_client.get("/premium/fwd?sym=BTC&fx=coinbase")
        assert r.status_code == 404

    async def test_empty_db_is_404(self, client) -> None:
        assert (await client.get("/premium/fwd?sym=BTC")).status_code == 404


class TestScanEndpoint:
    async def test_finds_best_of_each_direction(self, seeded_client) -> None:
        d = (await seeded_client.get("/premium/scan?limit=5")).json()

        assert d["scanned_coins"] == 3
        assert d["best_fwd"] is not None
        assert d["order"] == "asc"
        percents = [e["premium_percent"] for e in d["top_fwd"]]
        assert percents == sorted(percents)  # 오름차순 정렬
        # best 는 정렬과 무관하게 최대값
        assert d["best_fwd"]["premium_percent"] >= max(percents)

    async def test_empty_db_is_404(self, client) -> None:
        assert (await client.get("/premium/scan")).status_code == 404


class TestSlippageEndpoint:
    async def test_small_amount_has_no_slippage(self, seeded_client) -> None:
        d = (
            await seeded_client.get(
                "/slippage/upbit?symbol=BTC/KRW&side=buy&amount=1000000"
            )
        ).json()

        assert d["slippage_percent"] == 0.0
        assert d["levels_consumed"] == 1

    async def test_large_amount_creates_slippage(self, seeded_client) -> None:
        d = (
            await seeded_client.get(
                "/slippage/upbit?symbol=BTC/KRW&side=buy&amount=7000000"
            )
        ).json()

        assert d["slippage_percent"] > 0
        assert d["levels_consumed"] > 1
        assert d["average_price"] > d["best_price"]

    async def test_fills_match_upbit_tooltip_formula(self, seeded_client) -> None:
        d = (
            await seeded_client.get(
                "/slippage/upbit?symbol=BTC/KRW&side=buy&amount=7000000"
            )
        ).json()

        for f in d["fills"]:
            assert f["cumulative_average"] * f["cumulative_quantity"] == pytest.approx(
                f["cumulative_amount"], rel=1e-9
            )

    async def test_sell_side_slippage_is_positive(self, seeded_client) -> None:
        d = (
            await seeded_client.get(
                "/slippage/upbit?symbol=BTC/KRW&side=sell&quantity=0.05"
            )
        ).json()

        assert d["slippage_percent"] > 0
        assert d["average_price"] < d["best_price"]  # 매도는 평균가가 내려간다

    async def test_exhausting_stored_depth_warns(self, seeded_client) -> None:
        d = (
            await seeded_client.get(
                "/slippage/upbit?symbol=BTC/KRW&side=buy&amount=100000000"
            )
        ).json()

        assert d["depth_exhausted"] is True
        assert any("소진" in w for w in d["warnings"])

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("/slippage/upbit?symbol=BTC/KRW", 400),  # amount/quantity 없음
            ("/slippage/upbit?symbol=BTC/KRW&amount=1&quantity=1", 400),  # 둘 다
            ("/slippage/upbit?symbol=BTC/USDT&amount=1000", 404),  # 저장 마켓과 다른 quote
            ("/slippage/coinbase?symbol=BTC/KRW&amount=1000", 404),  # 없는 거래소
        ],
    )
    async def test_error_status_codes(self, seeded_client, url, expected) -> None:
        assert (await seeded_client.get(url)).status_code == expected

    async def test_empty_db_is_404(self, client) -> None:
        r = await client.get("/slippage/upbit?symbol=BTC/KRW&amount=1000")
        assert r.status_code == 404


class TestMatrixEndpoint:
    async def test_returns_per_coin_best_combinations(self, seeded_client) -> None:
        d = (await seeded_client.get("/matrix?amount_krw=1000000")).json()

        assert d["scanned_coins"] == 3
        assert d["scanned_combinations"] == 5
        assert set(d["dom_list"]) == {"upbit", "bithumb"}
        assert d["fx_list"] == ["binance"]

        first = d["coins"][0]
        assert first["sym"] == "XRP"  # 김프 내림차순 정렬 1등
        assert first["fwd"]["buy_exchange"] == "binance"
        for key in (
            "premium_percent",
            "total_slippage_percent",
            "depth_exhausted",
        ):
            assert key in first["fwd"]

    async def test_slippage_is_never_negative(self, seeded_client) -> None:
        d = (await seeded_client.get("/matrix?amount_krw=10000000")).json()

        for coin in d["coins"]:
            assert coin["fwd"]["total_slippage_percent"] >= -1e-9

    async def test_negative_amount_is_422(self, seeded_client) -> None:
        assert (await seeded_client.get("/matrix?amount_krw=-1")).status_code == 422

    async def test_empty_db_is_404(self, client) -> None:
        assert (await client.get("/matrix")).status_code == 404


class TestArbitrageEndpoint:
    async def test_auto_direction_is_profitable(self, seeded_client) -> None:
        d = (await seeded_client.get("/arbitrage?sym=BTC&amount=1000000")).json()

        assert d["profit_krw"] > 0  # 자동 선택은 이득 방향을 고른다
        assert d["buy"]["exchange"] == "binance"
        assert d["sell"]["exchange"] == "bithumb"

    async def test_fixed_direction_can_lose(self, seeded_client) -> None:
        d = (
            await seeded_client.get(
                "/arbitrage?sym=BTC&amount=1000000&direction=rev"
            )
        ).json()

        assert d["direction"] == "rev"
        assert d["profit_krw"] < 0  # 국내가 비싼 시나리오라 역방향은 손해

    async def test_larger_amount_reduces_capture(self, seeded_client) -> None:
        small = (await seeded_client.get("/arbitrage?sym=BTC&amount=1000000")).json()
        large = (await seeded_client.get("/arbitrage?sym=BTC&amount=10000000")).json()

        assert large["profit_percent"] < small["profit_percent"]
        assert (
            large["premium_capture_percent"] < small["premium_capture_percent"]
        )

    async def test_invalid_currency_is_400(self, seeded_client) -> None:
        r = await seeded_client.get("/arbitrage?sym=BTC&amount=1000&currency=EUR")
        assert r.status_code == 400

    async def test_single_venue_coin_is_409(self, seeded_client) -> None:
        """SOL 은 바이낸스에만 있다 → 비교 상대가 없어 409."""
        r = await seeded_client.get("/arbitrage?sym=SOL&amount=1000000")
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "no_arbitrage_opportunity"

    async def test_empty_db_is_404(self, client) -> None:
        assert (
            await client.get("/arbitrage?sym=BTC&amount=1000000")
        ).status_code == 404


class TestErrorBody:
    async def test_error_body_shape(self, client) -> None:
        body = (await client.get("/orderbook/coinbase?symbol=BTC/KRW")).json()

        assert set(body["error"]) == {"code", "message", "detail"}
        assert body["error"]["code"] == "unsupported_exchange"


class TestPlatformStatusEndpoint:
    """GET /history/status — 카운터 · 실패율 · 실패(결측) 구간."""

    @staticmethod
    async def _bump(db, exchange: str, ts: int, *, failed: bool) -> None:
        from app.db import repository as repo

        await repo.bump_platform_status(
            db,
            exchange=exchange,
            received_ts=ts,
            spot_market_count=3,
            futures_market_count=None,
            dw_failed=failed,
        )
        await db.commit()

    async def test_empty_db_is_404(self, client) -> None:
        assert (await client.get("/history/status")).status_code == 404

    async def test_fail_windows_are_merged_by_gap(self, db, client) -> None:
        """이웃 실패가 max_gap 이내면 한 구간, 넘게 벌어지면 쪼개진다."""
        import time as _time

        now = int(_time.time())
        # upbit — 20분 전 단발 실패 1회 + 방금 전 60초 간격 연속 실패 2회.
        await self._bump(db, "upbit", now - 1200, failed=True)
        await self._bump(db, "upbit", now - 120, failed=True)
        await self._bump(db, "upbit", now - 60, failed=True)
        await self._bump(db, "upbit", now, failed=False)
        # binance — 실패 없음.
        await self._bump(db, "binance", now, failed=False)

        d = (await client.get("/history/status")).json()
        assert d["retention_seconds"] == 86_400
        assert d["max_gap_seconds"] == 600

        by_ex = {p["exchange"]: p for p in d["platforms"]}
        upbit = by_ex["upbit"]
        assert upbit["dw_fail_count"] == 3 and upbit["update_count"] == 4
        assert upbit["dw_fail_rate"] == pytest.approx(0.75)

        # 1200초 전 단발과 (120초 전, 60초 전) 연속이 구간 2개로 나뉜다.
        w = upbit["dw_fail_windows"]
        assert len(w) == 2
        assert (w[0]["start_ts"], w[0]["end_ts"], w[0]["count"]) == (
            now - 1200,
            now - 1200,
            1,
        )
        assert w[0]["duration_seconds"] == 0
        assert (w[1]["start_ts"], w[1]["end_ts"], w[1]["count"]) == (
            now - 120,
            now - 60,
            2,
        )
        assert w[1]["duration_seconds"] == 60
        assert w[1]["start"].endswith("+09:00")  # 사람이 읽는 시각은 KST

        assert by_ex["binance"]["dw_fail_windows"] == []

    async def test_max_gap_param_splits_windows(self, db, client) -> None:
        import time as _time

        now = int(_time.time())
        await self._bump(db, "upbit", now - 120, failed=True)
        await self._bump(db, "upbit", now - 60, failed=True)

        # 간격 60초 > max_gap 30초 → 구간이 둘로 쪼개진다.
        d = (await client.get("/history/status?max_gap=30")).json()
        assert len(d["platforms"][0]["dw_fail_windows"]) == 2

    async def test_retention_prunes_old_events(self, db, client) -> None:
        """보존 기간(24시간)이 지난 실패 시각은 다음 갱신 때 지워진다."""
        import time as _time

        from app.db import repository as repo

        now = int(_time.time())
        old_ts = now - 86_400 - 100
        await self._bump(db, "upbit", old_ts, failed=True)
        assert (await repo.get_dw_fail_events(db, 0)) == {"upbit": [old_ts]}

        # 다음 갱신이 24시간 지난 행을 지운다 — 카운터는 그대로 누적이다.
        await self._bump(db, "upbit", now, failed=True)
        assert (await repo.get_dw_fail_events(db, 0)) == {"upbit": [now]}

        d = (await client.get("/history/status")).json()
        upbit = d["platforms"][0]
        assert upbit["dw_fail_count"] == 2  # 누적 카운터는 줄지 않는다
        assert [w["start_ts"] for w in upbit["dw_fail_windows"]] == [now]
