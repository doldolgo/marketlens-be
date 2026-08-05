"""애플리케이션 설정.

환경변수 또는 .env 파일로 주입한다. 호가 조회는 전부 public 엔드포인트라
API 키가 없어도 동작하며, 키는 향후 주문/잔고 기능을 붙일 때만 필요하다.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "MarketLens Backend"
    version: str = "0.1.0"

    # HTTP 클라이언트 튜닝 — ccxt 대비 속도 이점의 핵심 부분.
    # 커넥션 풀을 프로세스 전체에서 재사용해 TLS 핸드셰이크 비용을 제거한다.
    http_timeout: float = 3.0
    http_connect_timeout: float = 1.5
    http_max_connections: int = 100
    http_max_keepalive: int = 50

    # 호가창 기본 조회 깊이.
    default_orderbook_depth: int = 10

    # 원화 기준 거래소. 환율(USDT/KRW)과 프리미엄 계산의 KRW 쪽을 모두 여기서 가져온다.
    krw_reference_exchange: str = "upbit"
    krw_reference_quote: str = "KRW"
    #: 환율 산출에 쓰는 스테이블코인.
    fx_stablecoin: str = "USDT"
    #: 환율 캐시 유지 시간(초). 호가는 초 단위로 변하므로 짧게 잡는다.
    fx_cache_ttl: float = 1.0

    # 거래소 API 베이스 URL (장애 시 프록시/미러로 갈아끼울 수 있게 설정으로 노출)
    upbit_base_url: str = "https://api.upbit.com"
    binance_spot_base_url: str = "https://api.binance.com"
    binance_futures_base_url: str = "https://fapi.binance.com"


settings = Settings()
