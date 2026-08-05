"""거래소 추상 베이스 클래스.

새 거래소를 추가하려면 ``BaseExchange`` 를 상속해서
``to_native_symbol`` / ``_request_orderbook`` / ``_parse_orderbook`` 세 개만
구현하면 된다. HTTP 호출, 예외 변환, 지연시간 측정 같은 공통 로직은
전부 베이스에서 처리한다 (Template Method 패턴).
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any, ClassVar

import httpx

from app.core.errors import (
    ExchangeAPIError,
    ExchangeTimeoutError,
    UnsupportedMarketError,
)
from app.core.http import get_client
from app.models.orderbook import MarketType, OrderBook
from app.models.symbol import Symbol
from app.models.ticker import Ticker


class BaseExchange(ABC):
    """모든 거래소 커넥터의 공통 인터페이스."""

    #: 거래소 식별자 (URL path 와 응답에 쓰인다)
    id: ClassVar[str]
    #: 사람이 읽는 이름
    name: ClassVar[str]
    #: 이 거래소가 지원하는 결제 통화
    quote_currencies: ClassVar[frozenset[str]]
    #: 심볼에 quote 가 지정되지 않았을 때 사용할 기본 결제 통화
    default_quote: ClassVar[str]
    #: 이 거래소가 지원하는 시장 구분
    supported_market_types: ClassVar[frozenset[MarketType]] = frozenset({MarketType.SPOT})

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    @property
    def client(self) -> httpx.AsyncClient:
        return self._client or get_client()

    # ------------------------------------------------------------------
    # 하위 클래스가 구현해야 하는 부분
    # ------------------------------------------------------------------

    @abstractmethod
    def to_native_symbol(self, symbol: Symbol, market_type: MarketType) -> str:
        """통일 심볼을 거래소 네이티브 심볼로 변환한다."""

    @abstractmethod
    async def _request_orderbook(
        self, native_symbol: str, depth: int, market_type: MarketType
    ) -> Any:
        """거래소 호가 API 를 호출하고 원본 JSON 을 그대로 반환한다."""

    @abstractmethod
    def _parse_orderbook(
        self,
        raw: Any,
        *,
        symbol: Symbol,
        native_symbol: str,
        market_type: MarketType,
        depth: int,
        latency_ms: float,
    ) -> OrderBook:
        """원본 JSON 을 통일 OrderBook 모델로 변환한다."""

    @abstractmethod
    async def _request_ticker(self, native_symbol: str, market_type: MarketType) -> Any:
        """거래소 티커(현재가) API 를 호출하고 원본 JSON 을 그대로 반환한다."""

    @abstractmethod
    def _parse_ticker(
        self,
        raw: Any,
        *,
        symbol: Symbol,
        native_symbol: str,
        market_type: MarketType,
        latency_ms: float,
    ) -> Ticker:
        """원본 JSON 을 통일 Ticker 모델로 변환한다."""

    # ------------------------------------------------------------------
    # 공통 로직
    # ------------------------------------------------------------------

    def supports(self, symbol: Symbol, market_type: MarketType) -> bool:
        """이 거래소가 해당 마켓을 지원하는지 확인한다."""
        return (
            market_type in self.supported_market_types
            and symbol.quote in self.quote_currencies
        )

    def ensure_supported(self, symbol: Symbol, market_type: MarketType) -> None:
        """지원하지 않는 마켓이면 예외를 던진다."""
        if market_type not in self.supported_market_types:
            raise UnsupportedMarketError(
                f"{self.name}는 {market_type.value} 시장을 지원하지 않습니다.",
                detail={"exchange": self.id, "market_type": market_type.value},
            )
        if symbol.quote not in self.quote_currencies:
            raise UnsupportedMarketError(
                f"{self.name}는 {symbol.quote} 마켓을 지원하지 않습니다. "
                f"지원 통화: {', '.join(sorted(self.quote_currencies))}",
                detail={"exchange": self.id, "quote": symbol.quote},
            )

    async def fetch_orderbook(
        self,
        symbol: Symbol,
        *,
        depth: int = 10,
        market_type: MarketType = MarketType.SPOT,
    ) -> OrderBook:
        """호가창을 조회해 통일 모델로 반환한다 (Template Method).

        Args:
            symbol: 통일 심볼.
            depth: 조회할 호가 단계 수.
            market_type: 현물/선물 구분.

        Raises:
            UnsupportedMarketError: 지원하지 않는 마켓.
            ExchangeTimeoutError: 거래소 응답 지연.
            ExchangeAPIError: 거래소가 에러를 반환하거나 응답이 비정상.
        """
        self.ensure_supported(symbol, market_type)
        native_symbol = self.to_native_symbol(symbol, market_type)

        started = time.perf_counter()
        raw = await self._request_orderbook(native_symbol, depth, market_type)
        latency_ms = (time.perf_counter() - started) * 1000

        return self._parse_orderbook(
            raw,
            symbol=symbol,
            native_symbol=native_symbol,
            market_type=market_type,
            depth=depth,
            latency_ms=latency_ms,
        )

    async def fetch_ticker(
        self,
        symbol: Symbol,
        *,
        market_type: MarketType = MarketType.SPOT,
    ) -> Ticker:
        """마지막 체결가와 24시간 요약을 조회한다 (Template Method).

        Args:
            symbol: 통일 심볼.
            market_type: 현물/선물 구분.

        Raises:
            UnsupportedMarketError: 지원하지 않는 마켓.
            ExchangeTimeoutError: 거래소 응답 지연.
            ExchangeAPIError: 거래소가 에러를 반환하거나 응답이 비정상.
        """
        self.ensure_supported(symbol, market_type)
        native_symbol = self.to_native_symbol(symbol, market_type)

        started = time.perf_counter()
        raw = await self._request_ticker(native_symbol, market_type)
        latency_ms = (time.perf_counter() - started) * 1000

        return self._parse_ticker(
            raw,
            symbol=symbol,
            native_symbol=native_symbol,
            market_type=market_type,
            latency_ms=latency_ms,
        )

    async def _get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        """GET 요청 후 JSON 을 반환하고, 실패는 도메인 예외로 변환한다."""
        try:
            response = await self.client.get(url, params=params)
        except httpx.TimeoutException as exc:
            raise ExchangeTimeoutError(
                f"{self.name} API 응답 시간이 초과되었습니다.",
                detail={"exchange": self.id, "url": url},
            ) from exc
        except httpx.HTTPError as exc:
            raise ExchangeAPIError(
                f"{self.name} API 호출에 실패했습니다: {exc}",
                detail={"exchange": self.id, "url": url},
            ) from exc

        if response.status_code != 200:
            raise ExchangeAPIError(
                f"{self.name} API 가 {response.status_code} 를 반환했습니다.",
                detail={
                    "exchange": self.id,
                    "url": url,
                    "status_code": response.status_code,
                    "body": response.text[:500],
                },
            )

        try:
            return response.json()
        except ValueError as exc:
            raise ExchangeAPIError(
                f"{self.name} API 응답을 JSON 으로 파싱할 수 없습니다.",
                detail={"exchange": self.id, "url": url, "body": response.text[:500]},
            ) from exc

    def __repr__(self) -> str:
        return f"<{type(self).__name__} id={self.id}>"
