"""MarketLens 도메인 예외.

모든 예외는 ``MarketLensError`` 를 상속하며, HTTP 상태 코드를 스스로 알고 있다.
``app.main`` 의 exception handler 가 이를 일관된 JSON 에러 응답으로 변환한다.
"""

from __future__ import annotations


class MarketLensError(Exception):
    """모든 도메인 예외의 베이스."""

    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str, *, detail: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}


class InvalidSymbolError(MarketLensError):
    """심볼 문자열을 파싱할 수 없음."""

    status_code = 400
    code = "invalid_symbol"


class UnsupportedExchangeError(MarketLensError):
    """등록되지 않은 거래소 ID."""

    status_code = 404
    code = "unsupported_exchange"


class UnsupportedMarketError(MarketLensError):
    """거래소가 해당 마켓(심볼/시장구분)을 지원하지 않음."""

    status_code = 400
    code = "unsupported_market"


class InvalidRequestError(MarketLensError):
    """요청 값이 잘못됨 (지원하지 않는 통화 등)."""

    status_code = 400
    code = "invalid_request"


class NoArbitrageOpportunityError(MarketLensError):
    """지금 시장 상태에서는 거래소 간 차익 기회가 없음.

    잘못된 요청도 서버 오류도 아니고 **정상적인 시장 상태**다.
    409 로 구분해 클라이언트가 재시도 여부를 판단할 수 있게 한다.
    """

    status_code = 409
    code = "no_arbitrage_opportunity"


class ExchangeAPIError(MarketLensError):
    """거래소 API 가 에러 응답을 반환함."""

    status_code = 502
    code = "exchange_api_error"


class ExchangeTimeoutError(MarketLensError):
    """거래소 API 응답 시간 초과."""

    status_code = 504
    code = "exchange_timeout"


class MarketNotFoundError(MarketLensError):
    """거래소에 해당 마켓이 존재하지 않음 (상장되지 않은 코인 등)."""

    status_code = 404
    code = "market_not_found"


class MarketDataNotFoundError(MarketLensError):
    """DB 에 요청한 데이터가 없음.

    조회 API 는 거래소를 직접 부르지 않고 DB 만 읽는다. 아직 수집을 안 했거나
    (``POST /refresh``), 그 거래소에 상장되지 않은 코인이면 이 예외가 난다.
    """

    status_code = 404
    code = "market_data_not_found"
