"""애플리케이션 설정.

환경변수 또는 .env 파일로 주입한다. 호가·시세 조회는 전부 public 엔드포인트라
API 키가 없어도 동작한다. 키가 필요한 곳은 **입출금 가능 여부 조회** 뿐이며
(업비트·바이낸스 프라이빗 API), 키가 없으면 해당 값이 null 로 저장된다.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = "MarketLens Backend"
    version: str = "0.2.0"

    # ── 데이터베이스 ──────────────────────────────────────────────────
    #: PostgreSQL 접속 URL. postgresql:// 로 줘도 내부에서 asyncpg 드라이버로 바꾼다.
    #: 로컬 기본값은 docker-compose.dev.yml 이 띄우는 컨테이너와 일치한다.
    database_url: str = "postgresql://marketlens:marketlens@localhost:5432/marketlens"

    # HTTP 클라이언트 튜닝 — ccxt 대비 속도 이점의 핵심 부분.
    # 커넥션 풀을 프로세스 전체에서 재사용해 TLS 핸드셰이크 비용을 제거한다.
    http_timeout: float = 3.0
    http_connect_timeout: float = 1.5
    http_max_connections: int = 100
    http_max_keepalive: int = 50

    # 호가창 기본 조회 깊이.
    default_orderbook_depth: int = 10

    # ── 수집(refresh) 설정 ────────────────────────────────────────────
    #: 슬리피지 계산을 커버해야 하는 최대 금액 (원화).
    #: 호가를 DB 에 저장할 때 누적 체결 가능액이 이 금액에 도달할 때까지의
    #: 단계만 저장한다. 이보다 큰 금액의 슬리피지는 계산할 수 없다(depth_exhausted).
    orderbook_max_amount_krw: float = 1_000_000_000.0
    #: 바이낸스 depth 조회 단계 수 (심볼당). 허용값: 5/10/20/50/100/500/1000.
    binance_orderbook_depth: int = 100
    #: 입출금 실패 시각(dw_fail_events)의 보존 기간 (초). 수집 상태 창이
    #: 최근 이 기간의 실패 구간을 표시한다. 지난 행은 refresh 가 돌 때마다
    #: 지운다 — 별도 청소 잡이 없다.
    dw_fail_retention_seconds: int = 86_400
    #: 수집 루프 주기 (초). 앱 내부 스케줄러가 이 간격으로 돈다.
    collect_interval_seconds: float = 1.0
    #: DB 저장 루프 주기 (초). 수집 사이클과 **분리된 별도 태스크**가 이 간격으로
    #: 메모리의 현재 시세를 market_snapshots 에 내린다. 조회가 DB 를 보지 않으므로
    #: 수집이 이 쓰기를 기다릴 이유가 없다.
    persist_interval_seconds: float = 60.0
    #: premium_archive 적재 주기 (초). 저장 루프 **안에서** 이 주기로 따로 가드한다.
    #: persist 와 합치지 않은 이유 — 스냅샷은 행 수가 고정된 현재 상태 미러지만
    #: 아카이브는 append 전용이라 주기가 곧 DB 증가 속도다. 신선도와 증가량은
    #: 서로 다른 판단이므로 손잡이를 따로 둔다.
    archive_interval_seconds: float = 60.0
    #: 입출금 상태 갱신 주기 (초). 자주 바뀌지 않으므로 저빈도로 돌린다.
    wallet_refresh_seconds: float = 60.0
    #: 깊이를 조회할 김프 하한 (%). 이 미만이면 슬리피지를 계산할 이유가 없다.
    depth_watch_min_percent: float = 1.0
    #: 깊이 조회 코인 수 상한. 바이낸스 weight 예산의 안전장치 (12개 = 한도의 68%).
    depth_watch_max_count: int = 12
    #: POST /refresh 보호 토큰. 비우면 인증 없이 열린다 (로컬 개발용).
    #: 배포 시에는 반드시 설정할 것 — refresh 는 거래소 호출 수백 회가 나가는
    #: 비싼 작업이라, 외부인이 마음대로 트리거하면 rate limit 이 소진된다.
    refresh_token: str = ""

    # 원화 기준 거래소. 프리미엄 계산의 KRW 축이 될 수 있는 거래소.
    krw_reference_exchange: str = "upbit"
    krw_reference_quote: str = "KRW"
    #: 해외 거래소 마켓의 호가 통화 (바이낸스 USDT 마켓).
    overseas_quote: str = "USDT"

    # ── 김프/역프 기록 (premium_archive) ─────────────────────────────
    #: 대량 업데이트(scripts/bulk_archive.py)의 기본 대상 코인 목록 —
    #: CLI --bases 를 생략하면 이 값을 쓴다. 실시간 기록은 refresh 가
    #: 국내 상장 전 코인에 대해 자동으로 남기므로 여기 없어도 기록된다.
    history_bases: list[str] = ["BTC"]

    # 전종목 스캔 —
    #: 이 값을 넘는 프리미엄은 '의심' 으로 표시한다.
    #: 유동성 있는 코인에서 실제로 이만큼 벌어지는 일은 드물고, 대부분
    #: (1) 티커는 같지만 다른 프로젝트이거나 (2) 입출금이 막혀 가격이 따로 노는 경우다.
    scan_suspicious_percent: float = 5.0
    #: 티커 충돌이 확인된 코인. 스캔에서 아예 제외한다.
    scan_excluded_bases: list[str] = []

    #: /spreads 에서 스냅샷이 이 초 이상 오래되면 status=stale 로 표시한다.
    spread_stale_seconds: float = 5.0

    # 거래소 API 베이스 URL (장애 시 프록시/미러로 갈아끼울 수 있게 설정으로 노출)
    upbit_base_url: str = "https://api.upbit.com"
    bithumb_base_url: str = "https://api.bithumb.com"
    binance_spot_base_url: str = "https://api.binance.com"
    binance_futures_base_url: str = "https://fapi.binance.com"

    # ── API 키 (입출금 가능 여부 조회용) ──────────────────────────────
    #: 업비트 Open API 키. /v1/status/wallet 조회에 사용.
    upbit_api_key: str = ""
    upbit_secret_key: str = ""
    #: 바이낸스 API 키. /sapi/v1/capital/config/getall 조회에 사용.
    binance_api_key: str = ""
    binance_secret_key: str = ""


settings = Settings()
