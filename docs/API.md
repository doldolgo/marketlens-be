# MarketLens Backend API 문서

거래소 간 가격차(김프·역프)를 계산하는 백엔드. 데이터 흐름은 두 갈래다.

- **수집** — `POST /refresh` 가 거래소 공개 API 를 비동기로 동시 호출해
  시세·호가·입출금 상태·환율을 **PostgreSQL 에 저장**한다.
- **조회** — 그 외 모든 API 는 거래소를 직접 부르지 않고 **DB 만 읽어** 계산한다.

- Base URL (로컬): `http://localhost:8000`
- 인증: 조회 API 는 **없음**. `POST /refresh` 만 서버에 `REFRESH_TOKEN` 이
  설정된 경우 `X-Refresh-Token` 헤더가 필요하다 (로컬처럼 비어 있으면 검사 안 함).
- 응답 형식: `application/json`
- 대화형 문서: `http://localhost:8000/docs` (Swagger UI), `http://localhost:8000/redoc`

---

## 목차

1. [아키텍처 — 데이터는 어디서 오나](#1-아키텍처--데이터는-어디서-오나)
2. [빠른 시작](#2-빠른-시작)
3. [무엇을 조회할 수 있나 (한눈에 보기)](#3-무엇을-조회할-수-있나-한눈에-보기)
4. [핵심 개념](#4-핵심-개념)
5. [엔드포인트](#5-엔드포인트)
   - [POST /refresh](#post-refresh)
   - [GET /health](#get-health)
   - [GET /exchanges](#get-exchanges)
   - [GET /rate](#get-rate)
   - [GET /orderbook/{exchange_id}](#get-orderbookexchange_id)
   - [GET /compare](#get-compare)
   - [GET /premium](#get-premium)
   - [GET /premium/fwd · /premium/rev](#get-premiumfwd--get-premiumrev)
   - [GET /premium/scan](#get-premiumscan)
   - [GET /spreads](#get-spreads)
   - [GET /slippage/{exchange_id}](#get-slippageexchange_id)
   - [GET /matrix](#get-matrix)
   - [GET /arbitrage](#get-arbitrage)
6. [에러 응답](#6-에러-응답)
7. [수집기가 호출하는 원본 거래소 API](#7-수집기가-호출하는-원본-거래소-api)
8. [API 키 · 입출금 상태 조회](#8-api-키--입출금-상태-조회)
9. [새 거래소 추가하기 (자동 등록)](#9-새-거래소-추가하기-자동-등록)

---

## 1. 아키텍처 — 데이터는 어디서 오나

```
수집:  POST /refresh   →  거래소 공개 API 호출  →  PostgreSQL 에 저장
조회:  그 외 모든 API  →  PostgreSQL 만 읽음     (거래소 직접 호출 없음)
```

거래소를 실제로 호출하는 경로는 `POST /refresh` 하나뿐이다. 조회 API 의 응답
속도는 거래소 상태와 무관하고, 조회를 아무리 많이 해도 거래소 rate limit 을
소비하지 않는다. 대신 **조회 결과는 마지막 수집 시점의 스냅샷**이다.

### 테이블 — 2개뿐이다

#### `market_snapshots` — 거래소 × 코인 하나당 한 행

| 컬럼 | 설명 |
|---|---|
| `exchange`, `base` (PK) | 거래소 ID (`upbit`/`bithumb`/`binance`) × 코인 (`BTC`, …) |
| `native_symbol` | 거래소 원본 심볼 (`KRW-BTC`, `BTCUSDT`) |
| `quote` | 가격 통화 (`KRW` / `USDT`). 환산 없이 저장하므로 단위를 기록한다 |
| `price` | 현재 가격 — 마지막 체결가 (quote 통화 그대로) |
| `asks` / `bids` | 호가 `[[가격, 잔량], ...]`. asks 는 가격 오름차순, bids 는 내림차순 |
| `deposit_enabled` / `withdrawal_enabled` | 입금/출금 가능 여부. 확인 불가(키 없음 등)면 null |
| `price_timestamp` | 거래소가 준 시세 시각 (epoch ms) |
| `updated_at` | 이 행을 마지막으로 갱신한 시각 (DB 서버 시계) |

#### `krw_rates` — 국내 거래소당 한 행

| 컬럼 | 설명 |
|---|---|
| `exchange` (PK) | 국내 거래소 ID (`upbit` / `bithumb`) |
| `rate` | 그 거래소 `KRW-USDT` 마켓의 USDT 1개당 원화 (**마지막 체결가**) |
| `native_symbol` | 원본 마켓 심볼 (`KRW-USDT`) |
| `price_timestamp` | 거래소가 준 시세 시각 (epoch ms) |
| `updated_at` | 이 행을 마지막으로 갱신한 시각 |

### 저장 원칙

- **환산 없이 저장** — 가격·호가는 그 거래소 통화 그대로 저장한다
  (업비트·빗썸 = KRW, 바이낸스 = USDT). 원화 환산은 조회 시점에 `krw_rates`
  를 곱해서 한다.
- **호가는 금액 한도까지만** — 누적 체결 가능액이 서버 설정
  `ORDERBOOK_MAX_AMOUNT_KRW`(기본 10억원)에 도달하는 깊이까지만 저장한다.
  그보다 큰 금액의 슬리피지는 계산할 수 없고, 응답에 `depth_exhausted` 로
  표시된다.
- **없어진 코인은 삭제** — 이번 수집에 없는 코인(상장폐지 등)은 그 거래소의
  행에서 지운다. DB 는 항상 "마지막 수집 시점의 전체 스냅샷"이다.

### 데이터 신선도

조회 API 는 데이터가 오래돼도 그대로 계산한다. 대신 응답마다 신선도 필드가 있다.

| 필드 | 어디에 | 의미 |
|---|---|---|
| `data_updated_at` | 단일 스냅샷 기반 응답 (`/slippage` 등) | 그 스냅샷의 DB 갱신 시각 (epoch ms) |
| `data_oldest_at` / `data_newest_at` | 여러 스냅샷을 쓰는 응답 | 사용한 스냅샷 중 가장 오래된 / 최근 갱신 시각 |
| `updated_at` | `/fx` | 환율의 DB 갱신 시각 |

지금과의 차이가 크면 `POST /refresh` 로 갱신한다. 갱신 주기는 클라이언트
(또는 스케줄러)가 정한다 — 서버가 알아서 갱신하지 않는다.

테이블은 앱 기동 시 없으면 자동 생성된다 (`CREATE TABLE IF NOT EXISTS` 방식).

### 코드 지도 — 문서를 읽다가 코드를 확인하고 싶을 때

공통 기반 (모든 엔드포인트가 공유):

| 무엇 | 파일 |
|---|---|
| 설정 · 환경변수 목록 | `app/core/config.py` |
| 테이블 정의 (SQLAlchemy) | `app/db/models.py` |
| DB 읽기/쓰기 함수 — 조회 API 는 전부 이 모듈로 DB 를 읽는다 | `app/db/repository.py` |
| DB 엔진·세션 (FastAPI 의존성 `get_session`) | `app/db/database.py` |
| 호가 소진 계산 — 슬리피지·실현 수익률의 수학 | `app/services/orderbook_walk.py` |
| 거래소 커넥터 (수집기만 사용) | `app/exchanges/connectors/upbit.py` · `bithumb.py` · `binance.py` |
| 입출금 상태 조회 (프라이빗 API) | `app/exchanges/private/wallet_status.py` |
| 에러 정의 | `app/core/errors.py` |

엔드포인트별 (라우트 → 서비스 → 응답 모델 순서로 읽으면 된다):

| 엔드포인트 | 라우트 | 계산 로직 | 응답 모델 |
|---|---|---|---|
| `POST /refresh` | `app/api/routes/refresh.py` | `app/services/collector_service.py` | `app/models/refresh.py` |
| `GET /rate` | `app/api/routes/fx.py` | (라우트가 직접 DB 조회) | 같은 파일 안 |
| `GET /orderbook/{id}` | `app/api/routes/orderbook.py` | `repository.orderbook_from_snapshot` | `app/models/orderbook.py` |
| `GET /compare` | `app/api/routes/compare.py` | `app/services/comparison_service.py` | `app/models/comparison.py` |
| `GET /premium*` | `app/api/routes/premium.py` | `app/services/premium_service.py` | `app/models/premium.py` |
| `GET /premium/scan` | `app/api/routes/premium.py` | `app/services/scan_service.py` | `app/models/scan.py` |
| `GET /spreads` | `app/api/routes/spreads.py` | `app/services/spread_service.py` | `app/models/spread.py` |
| `GET /slippage/{id}` | `app/api/routes/slippage.py` | `app/services/slippage_service.py` | `app/models/slippage.py` |
| `GET /matrix` | `app/api/routes/matrix.py` | `app/services/matrix_service.py` | `app/models/matrix.py` |
| `GET /arbitrage` | `app/api/routes/arbitrage.py` | `app/services/arbitrage_service.py` | `app/models/arbitrage.py` |

---

## 2. 빠른 시작

```bash
# 1. 로컬 PostgreSQL + Adminer (localhost:5432, marketlens/marketlens/marketlens)
docker compose -f docker-compose.dev.yml up -d

# 2. 환경변수 — API 키는 입출금 상태 조회용 선택사항
cp .env.example .env

# 3. 서버 기동 (테이블 자동 생성)
pip install -r requirements.txt
uvicorn app.main:app --reload

# 4. 첫 수집 — DB 를 채운다
curl -X POST "http://localhost:8000/refresh"

# 5. 조회
curl "http://localhost:8000/premium/fwd?sym=BTC"
```

수집 전에 조회하면 `404 market_data_not_found` 가 난다 — 에러 메시지가
`POST /refresh` 를 먼저 하라고 안내한다.

DB 를 직접 보려면:

```bash
psql postgresql://marketlens:marketlens@localhost:5432/marketlens
# 또는
docker exec -it marketlens-db psql -U marketlens
```

Adminer (http://localhost:8080): 시스템 `PostgreSQL`, 서버 `db`,
사용자·비밀번호·데이터베이스 모두 `marketlens`.

---

## 3. 무엇을 조회할 수 있나 (한눈에 보기)

### 엔드포인트별 요약

| 엔드포인트 | 무엇을 하나 | 입력 | 대표 출력 |
|---|---|---|---|
| `POST /refresh` | **DB 갱신** — 거래소에서 수집해 저장 | 없음 | 거래소별 저장/삭제 수, 환율, 실패·경고 |
| `GET /health` | 서비스 생존 여부 | 없음 | `status`, `version` |
| `GET /exchanges` | 지원 거래소 목록 | 없음 | 거래소 ID, 이름, 결제 통화 |
| `GET /rate` | **USDT/KRW 환율 (저장값)** | 국내 거래소 | 거래소별 환율 + 갱신 시각 |
| `GET /orderbook/{id}` | 거래소 한 곳의 호가창 (DB 스냅샷) | 거래소, 심볼, 깊이 | 매수/매도 호가 배열 |
| `GET /compare` | 여러 거래소 가격을 한 통화로 환산해 비교 | 코인, 거래소들, 기준통화 | 거래소별 시세 + 스프레드 |
| `GET /premium` | **코인 검색 — 김프+역김프 동시** | 코인, 국내 거래소, 가격기준, 금액 | 두 방향 결과 + 더 나은 방향 |
| `GET /premium/fwd` | **김프** — 해외 매수 → 국내 매도 수익률 | 코인, 거래소들, 가격기준, 금액 | 거래소별 수익률 %, 원화 차익 |
| `GET /premium/rev` | **역김프** — 국내 매수 → 해외 매도 수익률 | 코인, 거래소들, 가격기준, 금액 | 거래소별 수익률 %, 원화 차익 |
| `GET /premium/scan` | **전종목 스캔** — 방향별 수익률 1등 | 거래소들, 가격기준, 유동성 필터 | 방향별 1등과 상위 목록 |
| `GET /spreads` | **스프레드 테이블** — 전 페어 김프/역프 한 번에 (FE 계약) | 없음 | 페어별 fwd/rev/usd/유동성/신선도 |
| `GET /slippage/{id}` | **슬리피지** — 시장가 거래 시 평균 체결가 악화 | 거래소, 심볼, 방향, 금액/수량 | 평균 체결가, 슬리피지 %, 단계별 체결 |
| `GET /matrix` | **전 코인 매트릭스** — 코인별 최대 김프·최대 역프 | 금액 1개 | 코인별 최적 조합 + 실현 수익률 + 입출금 가능 여부 |
| `GET /arbitrage` | **N원 넣으면 실제로 얼마 남나** | 코인, 금액, 통화, 방향 | 매수/매도처, 슬리피지, 실수익 |

### 조회 가능한 데이터 범위

| 항목 | 가능 여부 | 비고 |
|---|:---:|---|
| 호가 (매수·매도 다단계) | ✅ | 저장된 깊이 안에서 — 국내 30단계, 바이낸스 기본 100단계, `ORDERBOOK_MAX_AMOUNT_KRW` 커버분까지 |
| 마지막 체결가 (현재가) | ✅ | 수집 시점의 값 |
| USDT/KRW 환율 | ✅ | `krw_rates` 저장값 — 국내 거래소별 마지막 체결가 **하나** |
| 국내 거래소 선택 (업비트/빗썸) | ✅ | `dom` 파라미터 |
| 김치 프리미엄 / 역김프 | ✅ | `/premium/fwd` · `/premium/rev` |
| 전종목 스캔 · 전 코인 매트릭스 | ✅ | DB 만 읽으므로 거래소 호출 0 |
| 금액 기준 실제 체결 시뮬레이션 | ✅ | `/arbitrage` — 저장된 호가창을 소진시켜 계산 |
| 슬리피지 | ✅ | `/slippage/{id}` (단일 거래소), `/matrix` · `/arbitrage` (양쪽) |
| **입금/출금 가능 여부** | ✅ | 수집 시 저장. 업비트·바이낸스는 API 키 필요, 없으면 null |
| 선물 시세 | ❌ | DB 는 **현물만** 저장한다 |
| 시가·고가·저가·등락률·거래량 | ❌ | 저장하지 않음 |
| 개별 체결 내역 / 캔들 | ❌ | 미구현 |
| 거래·출금 수수료 반영 | ❌ | 모든 수익 계산이 **수수료 미반영 이론값** |
| 잔고 · 주문 · 입출금 실행 | ❌ | 미구현 |

### 코인·마켓 범위 (수집 대상)

| 거래소 | 수집 범위 |
|---|---|
| 업비트 (`upbit`) | KRW 마켓 **전종목** |
| 빗썸 (`bithumb`) | KRW 마켓 **전종목** |
| 바이낸스 (`binance`) | USDT 마켓 중 **국내(업비트·빗썸)에 상장된 코인만** |

DB 에 없는 (거래소 × 코인) 조합을 조회하면 `404 market_data_not_found` 가 난다 —
수집을 안 했거나, 그 거래소에 상장되지 않은 코인이다.

---

## 4. 핵심 개념

### 통일 심볼 (Unified Symbol)

거래소마다 심볼 표기가 다르다. 이 API 는 **`BASE/QUOTE`** 한 가지 형식만 받는다.

| 요청 심볼 | 업비트 저장 마켓 | 바이낸스 저장 마켓 |
|---|---|---|
| `BTC/KRW` | `KRW-BTC` | (없음 — 바이낸스는 USDT 마켓만 저장) |
| `BTC/USDT` | (없음) | `BTCUSDT` |

구분자는 `/`, `-`, `_` 를 모두 허용하며 대소문자를 가리지 않는다.

DB 는 거래소당 한 마켓(국내 = KRW, 바이낸스 = USDT)만 저장하므로, `/orderbook`
등에서 QUOTE 가 저장 마켓과 다르면 404 로 올바른 심볼을 안내한다.

### 매수호가 / 매도호가

| 필드 | 의미 |
|---|---|
| `bids` | 매수 호가. 가격 **내림차순**. `bids[0]` = 최우선 매수호가 → **내가 지금 팔면 받는 가격** |
| `asks` | 매도 호가. 가격 **오름차순**. `asks[0]` = 최우선 매도호가 → **내가 지금 사면 내는 가격** |

즉 차익거래는 `asks[0]` 이 가장 싼 거래소에서 사서, `bids[0]` 이 가장 비싼 거래소에서 판다.

### 가격 기준

프리미엄 계산의 가격은 **실제로 체결되는 쪽 호가**다 — 살 때는 매도호가(ask),
팔 때는 매수호가(bid). 별도 옵션 없이 항상 이 기준으로 계산한다.

방향마다 집는 호가가 다르므로 김프/역김프 값은 서로 독립적이고, 스프레드를
양쪽에서 지불한 값이므로 겉보기 가격 차이보다 항상 보수적으로 나온다.

금액 기반 계산(호가를 훑는 슬리피지 반영)은 `/matrix` 와 `/arbitrage` 가 담당한다.

### 환율

업비트·빗썸은 KRW, 바이낸스는 USDT 로 가격을 매기므로 그대로는 비교할 수 없다.
환율은 **`krw_rates` 에 저장된 국내 거래소별 `KRW-USDT` 마켓 마지막 체결가**를
쓴다. 은행 고시 USD/KRW 가 아니라 실제 국내 시장에서 거래되는 테더 가격이다 —
은행 환율과의 차이가 곧 **테더 프리미엄**이다.

- **거래소마다 값이 다르다.** 업비트 `KRW-USDT` 와 빗썸 `KRW-USDT` 는 서로 다른
  시장이다. 그래서 국내 거래소별로 한 행씩 저장하고, 원화 환산이 필요한 계산은
  **해당 국내 거래소의 환율**을 쓴다. 그 거래소의 환율이 DB 에 없으면 기준
  거래소(`upbit`, 설정 `KRW_REFERENCE_EXCHANGE`) 환율로 폴백한다.
- **저장값은 마지막 체결가 하나뿐이다.** 코인 가격은 호가에서 뽑지만 환율은
  저장된 체결가 하나를 쓴다.
- 캐시가 아니라 저장값이므로 `POST /refresh` 전에는 바뀌지 않는다.
  각 응답의 `rate_updated_at` (또는 `/fx` 의 `updated_at`)으로 신선도를 확인한다.

---

## 5. 엔드포인트

### POST /refresh

**DB 갱신 — 이 백엔드에서 거래소 API 를 실제로 호출하는 유일한 엔드포인트.**
나머지 모든 조회 API 는 여기서 저장한 DB 를 읽는다.

#### 수집 대상

| 데이터 | 출처 | 저장 위치 |
|---|---|---|
| KRW 전종목 현재가 + 호가 | 업비트 · 빗썸 (전종목 일괄 조회) | `market_snapshots` |
| USDT 마켓 현재가 + 호가 | 바이낸스 (국내 상장 코인만, 심볼별 depth) | `market_snapshots` |
| 입출금 가능 여부 | 업비트 · 바이낸스 (API 키 필요) · 빗썸 (public) | `market_snapshots` |
| KRW-USDT 환율 (마지막 체결가) | 업비트 · 빗썸 | `krw_rates` |

- 가격·호가는 **환산 없이 그 거래소 통화 그대로** 저장된다.
- 호가는 `ORDERBOOK_MAX_AMOUNT_KRW`(기본 10억원)의 체결을 커버하는 깊이까지만
  저장된다. 바이낸스 호가는 USDT 기준이므로 업비트 환율로 환산한 금액을 쓴다.
- 이번 수집에 없는 코인은 지워진다 (응답의 `deleted`).
- API 키가 없으면 입출금 가능 여부만 null 로 저장되고 나머지는 정상 수집된다
  (`warnings` 에 표시).
- 부분 실패를 허용한다 — 개별 거래소·심볼 조회 실패는 `failures` 에 담기고
  HTTP 는 200 으로 나머지 결과를 반환한다.
- **거래소 수집이 통째로 실패하면 그 거래소의 기존 스냅샷을 유지한다.**
  빈 결과로 덮어써서 일시적 API 장애 한 번에 데이터가 전부 지워지는 일을
  막는다. 이 경우 `warnings` 에 표시되고, 낡은 데이터인지는 `updated_at`
  으로 판별하면 된다.
- **동시 호출은 서버에서 직렬화된다** — 두 refresh 가 겹쳐 들어와도 한 번에
  하나씩 실행된다.

```bash
curl -X POST "http://localhost:8000/refresh"

# 서버에 REFRESH_TOKEN 이 설정된 경우 (배포 환경)
curl -X POST "http://localhost:8000/refresh" -H "X-Refresh-Token: <토큰>"
```

서버 `.env` 에 `REFRESH_TOKEN` 이 설정돼 있으면 헤더가 없거나 틀릴 때 `401` 이
난다. 로컬처럼 비어 있으면 검사하지 않는다. refresh 는 거래소 호출이 수백 회
나가는 비싼 작업이라, 외부에 노출되는 배포에서는 반드시 설정한다.

#### 응답 `200 OK`

```json
{
  "snapshots": [
    { "exchange": "upbit", "saved": 189, "deleted": 0,
      "wallet_status_available": true, "mode": "bulk" },
    { "exchange": "bithumb", "saved": 313, "deleted": 1,
      "wallet_status_available": true, "mode": "bulk" },
    { "exchange": "binance", "saved": 202, "deleted": 0,
      "wallet_status_available": false, "mode": "per_symbol" }
  ],
  "krw_rates": [
    { "exchange": "bithumb", "rate": 1407.0 },
    { "exchange": "upbit", "rate": 1406.0 }
  ],
  "total_saved": 704,
  "failures": [],
  "warnings": [
    "binance 입출금 상태를 건너뜀 — BINANCE_API_KEY / BINANCE_SECRET_KEY 가 비어 있습니다. (해당 거래소의 deposit_enabled / withdrawal_enabled 는 null)"
  ],
  "total_calls": 215,
  "fetched_at": 1786370137000,
  "elapsed_ms": 2841.55
}
```

| 필드 | 설명 |
|---|---|
| `snapshots[].saved` / `deleted` | 저장(UPSERT)한 코인 수 / 이번 수집에 없어서 지운 코인 수 |
| `snapshots[].wallet_status_available` | 입출금 가능 여부를 채웠는지. false 면 키가 없거나 조회 실패 → null 저장 |
| `snapshots[].mode` | `bulk`=전종목 일괄 조회 (업비트·빗썸), `per_symbol`=심볼별 조회 (바이낸스) |
| `krw_rates` | 저장한 국내 거래소별 KRW-USDT 환율 |
| `failures` | 수집하지 못한 항목 (`exchange`, `sym`, `error_code`, `message`) |
| `warnings` | 키 없음, 환율 조회 실패 등 주의 사항 |
| `total_calls` | 이번 갱신에서 나간 **거래소 HTTP 호출 수** (실측) |

#### 호출 비용과 주기

업비트·빗썸은 전종목 일괄 조회라 호출이 몇 회로 끝나지만, 바이낸스는 depth 를
일괄로 주는 엔드포인트가 없어 **국내 상장 교집합 코인마다 1회씩** 호출한다
(동시 실행 수는 `REFRESH_CONCURRENCY`, 기본 20 으로 제한). 한 번의 refresh 가
대략 **200회 안팎**의 호출을 만들므로, 초 단위로 반복 호출하면 거래소
rate limit (바이낸스 분당 6,000 weight, 업비트 그룹당 초당 10회)에 걸릴 수 있다.
실제 호출 수는 응답의 `total_calls` 로 확인한다.

---

### GET /health

서비스 상태 확인.

```bash
curl "http://localhost:8000/health"
```

```json
{ "status": "ok", "version": "0.2.0" }
```

---

### GET /exchanges

지원 거래소 목록. **커넥터 파일을 추가하면 이 목록에 자동으로 나타난다.**

```bash
curl "http://localhost:8000/exchanges"
```

```json
[
  {
    "id": "binance",
    "name": "바이낸스",
    "default_quote": "USDT",
    "quote_currencies": ["BNB", "BTC", "ETH", "FDUSD", "USDC", "USDT"],
    "market_types": ["futures", "spot"]
  },
  {
    "id": "bithumb",
    "name": "빗썸",
    "default_quote": "KRW",
    "quote_currencies": ["KRW"],
    "market_types": ["spot"]
  },
  {
    "id": "upbit",
    "name": "업비트",
    "default_quote": "KRW",
    "quote_currencies": ["BTC", "KRW", "USDT"],
    "market_types": ["spot"]
  }
]
```

| 필드 | 설명 |
|---|---|
| `id` | 다른 엔드포인트에서 거래소를 지정할 때 쓰는 값 |
| `default_quote` | 이 거래소의 기본 결제 통화 |
| `quote_currencies` / `market_types` | **커넥터의 능력** 기준. DB 에는 현물 한 마켓(국내 KRW · 바이낸스 USDT)만 저장된다 |

---

### GET /rate

**USDT/KRW 환율 조회 — DB 저장값.**

거래소를 직접 호출하지 않는다. 반환값은 `POST /refresh` 가 `krw_rates` 에
저장해둔 **국내 거래소별 `KRW-USDT` 마켓 마지막 체결가**다.

#### 요청

| 파라미터 | 타입 | 기본값 | 설명 |
|---|---|---|---|
| `exchange` | string | (전체) | 환율을 조회할 **국내 거래소** ID (`upbit` \| `bithumb`). 생략하면 저장된 전체. 등록되지 않은 ID 면 `404 unsupported_exchange`, 등록됐지만 환율이 아직 없으면 `404 market_data_not_found` |

```bash
curl "http://localhost:8000/rate"
curl "http://localhost:8000/rate?exchange=bithumb"
```

#### 응답 `200 OK`

```json
{
  "rates": [
    { "exchange": "bithumb", "rate": 1407.0, "updated_at": 1786370137012 },
    { "exchange": "upbit", "rate": 1406.0, "updated_at": 1786370137012 }
  ],
  "fetched_at": 1786370950000
}
```

| 필드 | 설명 |
|---|---|
| `rate` | USDT 1개의 원화 가격 (마지막 체결가) |
| `updated_at` | 이 환율을 DB 에 저장한 시각 (epoch ms) — **데이터 신선도 기준** |

빗썸과 업비트의 `KRW-USDT` 는 서로 다른 시장이라 값이 다르다. 원화 환산이
필요한 조회 API 들은 해당 국내 거래소의 환율을 쓰되, 없으면 기준 거래소
(`upbit`) 환율로 폴백한다.

---

### GET /orderbook/{exchange_id}

거래소 한 곳의 호가창을 **DB 스냅샷에서** 조회한다.

#### 요청

| 파라미터 | 위치 | 타입 | 필수 | 기본값 | 설명 |
|---|---|---|:---:|---|---|
| `exchange_id` | path | string | ✅ | — | `upbit` \| `bithumb` \| `binance` |
| `symbol` | query | string | ✅ | — | 통일 심볼. QUOTE 는 저장 마켓과 일치해야 한다 (국내=`KRW`, 바이낸스=`USDT`) |
| `depth` | query | int (1–100) | | `10` | 반환할 호가 단계 수. **저장된 깊이 안에서 자르기만 한다** — 저장 단계보다 큰 값을 줘도 저장된 만큼만 |

```bash
curl "http://localhost:8000/orderbook/upbit?symbol=BTC/KRW&depth=3"
curl "http://localhost:8000/orderbook/binance?symbol=BTC/USDT&depth=3"
```

#### 응답 `200 OK`

```json
{
  "exchange": "upbit",
  "symbol": "BTC/KRW",
  "base": "BTC",
  "quote": "KRW",
  "bids": [
    { "price": 91553000.0, "size": 0.13771879 },
    { "price": 91552000.0, "size": 0.05496266 }
  ],
  "asks": [
    { "price": 91587000.0, "size": 0.01396964 },
    { "price": 91606000.0, "size": 0.01564542 }
  ],
  "timestamp": 1785941602846,
  "data_updated_at": 1786370137012
}
```

| 필드 | 설명 |
|---|---|
| `bids` / `asks` | `{price, size}` 배열. `price` 는 quote 통화, `size` 는 base 통화 |
| `timestamp` | **수집 시점에** 거래소가 준 시세 시각 (epoch ms) |
| `data_updated_at` | 이 스냅샷의 DB 갱신 시각 (epoch ms) — **데이터 신선도 기준.** 오래됐으면 `POST /refresh` |

- 스냅샷이 없으면 `404 market_data_not_found` — 수집을 안 했거나 미상장 코인.
- QUOTE 가 저장 마켓과 다르면 404 로 올바른 심볼을 안내한다
  (예: `BTC/USDT` 를 업비트에 요청하면 `BTC/KRW` 로 다시 요청하라고 알려줌).

---

### GET /compare

여러 거래소의 같은 코인 가격(마지막 체결가)을 하나의 통화로 환산해 비교하고,
**차익 스프레드**를 계산한다. DB 스냅샷만 읽는다.

환율은 DB 의 `krw_rates` 를 사용한다 — KRW 환산은 기준 국내 거래소(업비트)
환율을 곱하고, USDT 환산은 그 국내 거래소 자기 환율로 나눈다 (없으면 기준 환율).
응답의 가격은 전부 `common_currency` 로 환산된 최종값이다.

#### 요청

| 파라미터 | 타입 | 필수 | 기본값 | 설명 |
|---|---|:---:|---|---|
| `sym` | string | ✅ | — | 비교할 코인 심볼 (`BTC`, `ETH`, `XRP` …) |
| `exchanges` | string[] | | 전체 | 비교할 거래소 ID. 반복 지정 (`&exchanges=upbit&exchanges=binance`). 생략하면 스냅샷이 있는 전체 거래소 |
| `common_currency` | string | | `KRW` | 환산 기준 통화. `KRW` 또는 `USDT` |

```bash
curl "http://localhost:8000/compare?sym=BTC"
curl "http://localhost:8000/compare?sym=ETH&common_currency=USDT"
```

#### 응답 `200 OK`

```json
{
  "sym": "BTC",
  "common_currency": "KRW",
  "usdt_krw_rate": 1406.0,
  "rate_exchange": "upbit",
  "quotes": [
    {
      "exchange": "binance",
      "quote_currency": "USDT",
      "price": 90543588.0,
      "best_bid": 90543573.94,
      "best_ask": 90543602.06,
      "data_updated_at": 1786370137012
    },
    {
      "exchange": "upbit",
      "quote_currency": "KRW",
      "price": 90553000.0,
      "best_bid": 90553000.0,
      "best_ask": 90587000.0,
      "data_updated_at": 1786370137012
    }
  ],
  "missing_exchanges": [],
  "spread": {
    "buy_exchange": "binance",
    "buy_price": 90543602.06,
    "sell_exchange": "upbit",
    "sell_price": 90553000.0,
    "absolute": 9397.94,
    "percent": 0.0104
  },
  "data_oldest_at": 1786370137012,
  "data_newest_at": 1786370137012,
  "fetched_at": 1786370950000,
  "elapsed_ms": 3.2
}
```

| 필드 | 설명 |
|---|---|
| `usdt_krw_rate` / `rate_exchange` | 기준 환율과 그 출처 국내 거래소. 저장된 환율이 없으면 null |
| `quotes` | 거래소별 시세 (**전부 `common_currency` 로 환산된 값**). `price` 오름차순(= 싼 거래소가 먼저) 정렬 |
| `quotes[].price` / `best_bid` / `best_ask` | 마지막 체결가 · 최우선 매수/매도호가 (공통 통화 환산) |
| `quotes[].quote_currency` | 원래 결제 통화 (KRW / USDT) — 환산 전 단위 표시용 |
| `missing_exchanges` | 요청한 거래소 중 이 코인의 스냅샷이 없는 곳 |
| `spread` | 최저 매수처 ↔ 최고 매도처 차이. 호가가 저장된 거래소가 2곳 미만이면 null |

> ⚠️ `spread` 는 수수료·출금비용·전송시간·호가 잔량을 반영하지 않은 이론적
> 가격차다. 실제 금액 기준 수익은 `/arbitrage` 로 확인할 것.

---

### GET /premium

**코인 하나를 검색하면 김프와 역김프를 동시에** 반환한다.

`/premium/fwd` 와 `/premium/rev` 를 각각 부르는 것과 결과가 같지만 왕복이
한 번으로 줄어들고, 같은 DB 스냅샷을 읽으므로 두 방향이 **같은 시점 기준**으로
계산된다.

#### 요청

| 파라미터 | 타입 | 필수 | 기본값 | 설명 |
|---|---|:---:|---|---|
| `sym` | string | ✅ | — | 검색할 코인 심볼 (`BTC`, `ETH` …) |
| `dom` | string | | `upbit` | **국내 거래소** (`upbit` \| `bithumb`). 김프의 원화 축. 환율도 이 거래소의 저장값을 쓴다 |
| `fx` | string[] | | 전체 | 비교할 **해외** 거래소. 생략하면 DB 에 USDT 스냅샷이 있는 전체 (국내 기준 거래소 제외) |

```bash
curl "http://localhost:8000/premium?sym=BTC"
curl "http://localhost:8000/premium?sym=BTC&dom=bithumb"
curl "http://localhost:8000/premium?sym=XRP"
```

#### 응답 `200 OK`

```json
{
  "sym": "BTC",
  "fwd":  { "direction": "fwd", "premiums": [ "..." ] },
  "rev": { "direction": "rev", "premiums": [ "..." ] },
  "best_direction": "rev",
  "best_premium_percent": 0.0145,
  "data_oldest_at": 1786370137012,
  "data_newest_at": 1786370137012,
  "fetched_at": 1786370950000,
  "elapsed_ms": 5.1
}
```

`fwd` / `rev` 는 각각 `/premium/fwd` · `/premium/rev` 응답과
**동일한 형태**다.

| 필드 | 설명 |
|---|---|
| `best_direction` | 두 방향 중 수익률이 높은 쪽. 계산 불가면 null |
| `best_premium_percent` | 그 방향의 수익률 (%) |

> ⚠️ **`best_direction` 은 "이득이 나는 방향" 이 아니라 "덜 나쁜 쪽" 일 수 있다.**
> 둘 다 손해일 때도 값이 채워지므로, 해당 방향의 `profitable` 을 반드시 확인할 것.

`dom` 을 바꾸면 국내 가격뿐 아니라 **환율도 그 거래소 것**으로 바뀐다.
같은 코인·같은 시각인데 결과가 다를 수 있다 — 국내 가격도 환율도 거래소마다
다르기 때문이다. 원화 거래소가 아닌 곳(`binance`)을 지정하면 `400`.

---

### GET /premium/fwd · GET /premium/rev

**방향별로 엔드포인트가 나뉘어 있다.** 두 방향은 부호만 뒤집은 값이 아니라
**서로 다른 거래**이기 때문이다.

| 엔드포인트 | 거래 방향 | 언제 이득인가 |
|---|---|---|
| `GET /premium/fwd` | **해외 매수 → 국내 매도** | 국내가 비쌀 때 |
| `GET /premium/rev` | **국내 매수 → 해외 매도** | 해외가 비쌀 때 |

```
김프   수익률 = 국내 매도가 / 해외 매수가(원화 환산) - 1
역김프 수익률 = 해외 매도가(원화 환산) / 국내 매수가 - 1
```

**양수면 그 방향이 이득, 음수면 손해**다. `profitable` 필드로도 알려준다.

가격은 실제로 체결되는 쪽 호가를 쓰므로 방향마다 집는 호가가 다르다.

| 방향 | 해외 쪽 | 국내 쪽 |
|---|---|---|
| `fwd` (해외 매수 → 국내 매도) | `ask` | `bid` |
| `rev` (국내 매수 → 해외 매도) | `bid` | `ask` |

스프레드가 넓은 종목에서는 **두 방향이 동시에 음수**일 수 있고, 그게 정상이다
(스프레드가 가격차를 다 먹은 상태).

#### 요청

두 엔드포인트가 파라미터를 공유한다.

| 파라미터 | 타입 | 필수 | 기본값 | 설명 |
|---|---|:---:|---|---|
| `sym` | string | ✅ | — | 조회할 코인 심볼 |
| `dom` | string | | `upbit` | 국내 거래소. 환율도 이 거래소의 `krw_rates` 저장값 (없으면 기준 거래소로 폴백) |
| `fx` | string[] | | 전체 | 비교할 **해외** 거래소 ID. 반복 지정 가능. 생략하면 DB 에 USDT 스냅샷이 있는 전체 (국내 기준 거래소 제외) |

```bash
# 김프 — 해외에서 사와서 국내에 팔 때
curl "http://localhost:8000/premium/fwd?sym=BTC"

# 역김프 — 국내에서 사서 해외에 팔 때
curl "http://localhost:8000/premium/rev?sym=BTC"
```

#### 응답 `200 OK`

```json
{
  "sym": "BTC",
  "direction": "fwd",
  "dom": "upbit",
  "dom_price": 91352000.0,
  "usdt_krw_rate": 1406.0,
  "rate_updated_at": 1786370137012,
  "premiums": [
    {
      "exchange": "binance",
      "name": "바이낸스",
      "usd": 64950.3,
      "premium_percent": 0.0349,
      "premium_krw": 31878.2,
      "profitable": true,
      "data_updated_at": 1786370137012
    }
  ],
  "failures": [],
  "data_oldest_at": 1786370137012,
  "data_newest_at": 1786370137012,
  "fetched_at": 1786370950000,
  "elapsed_ms": 4.2
}
```

##### 최상위 필드

| 필드 | 설명 |
|---|---|
| `direction` | `fwd` \| `rev` |
| `dom` / `dom_price` | 국내 거래소와 그 가격 (KRW). 김프면 bid, 역김프면 ask 기준 |
| `usdt_krw_rate` / `rate_updated_at` | 적용 환율 — `krw_rates` 저장값 (마지막 체결가 하나) 과 그 DB 저장 시각 |
| `premiums` | 해외 거래소별 결과. **수익률 내림차순** |
| `failures` | 스냅샷이 없거나 가격을 뽑지 못한 거래소 (부분 실패 허용) |
| `data_oldest_at` / `data_newest_at` | 사용한 스냅샷의 갱신 시각 범위 |

##### `premiums[]` 필드

| 필드 | 설명 |
|---|---|
| `usd` | 해외 가격 (USDT). 김프면 ask, 역김프면 bid 기준. 원화 환산은 `usdt_krw_rate` 를 곱하면 된다 |
| `premium_percent` | **이 방향의 수익률 (%)**. 양수=이득, 음수=손해 |
| `premium_krw` | 코인 1개당 원화 차익 |
| `profitable` | `premium_percent > 0` |
| `data_updated_at` | 이 해외 스냅샷의 DB 저장 시각 |

#### 실패 조건

| 실패한 것 | 결과 |
|---|---|
| 국내 스냅샷 없음 | ❌ **404** — 기준이 없으면 계산 불가 |
| 환율 없음 (해당 거래소도, 기준 거래소도) | ❌ **404** |
| 개별 해외 거래소 스냅샷 없음 | ✅ `failures` 에 담고 나머지는 정상 반환 |
| 저장된 호가가 비어 있음 | 국내 쪽이면 ❌ **404**, 해외 쪽이면 ✅ `failures` |
| `dom` 이 원화 거래소가 아님 | ❌ **400** `invalid_request` |

> ⚠️ 거래 수수료·출금 수수료·전송 시간은 반영하지 않은 이론값이다.
> 실제 금액을 넣었을 때의 수익은 `/arbitrage`, 전 코인 비교는 `/matrix` 로 확인할 것.

---

### GET /premium/scan

국내에 상장된 **모든 코인**을 훑어 두 방향 각각의 수익률 1등을 찾는다.

| 필드 | 내용 |
|---|---|
| `best_fwd` | **김프 1등** — 해외 매수 → 국내 매도 수익률 최대 |
| `best_rev` | **역김프 1등** — 국내 매수 → 해외 매도 수익률 최대 |
| `top_fwd` / `top_rev` | 방향별 목록 (`order` 방향으로 정렬) |

수익률 계산식은 `/premium/fwd` · `/premium/rev` 와 **완전히 동일**하다.
DB 의 국내(KRW) 전종목 × 해외(USDT) 스냅샷 교집합을 돌 뿐이므로 **거래소 호출은
0** 이다.

#### 요청

| 파라미터 | 타입 | 기본값 | 설명 |
|---|---|---|---|
| `dom` | string | `upbit` | 국내 거래소 (`upbit` \| `bithumb`) |
| `fx` | string[] | 전체 | 비교할 해외 거래소 |
| `min_liquidity_krw` | float | `0` | 저장된 최우선 호가의 체결 가능 금액(잔량 × 가격)이 이보다 작은 조합은 제외 (원화). `0` 이면 필터 없음 |
| `limit` | int (1–100) | `10` | 방향별 목록 개수 |
| `order` | enum | **`asc`** | `top_*` 목록의 정렬 방향. **어느 방향이든 목록에 담기는 것은 항상 수익률 상위 `limit` 개**이고, 그 안에서만 정렬이 바뀐다 (`asc`=오름차순 → 1등이 마지막에). `best_*` 는 정렬과 무관하게 항상 최대값 |

가격은 체결되는 쪽 호가를 쓰므로 오래된 체결가가 1등으로 올라오는 유령 값이
걸러진다. `min_liquidity_krw` 로 얇은 호가까지 함께 걸러내는 것을 권한다.
금액 기반 전종목 계산은 `GET /matrix` 가 담당한다.

#### ⚠️ 큰 프리미엄은 대부분 함정이다

전종목 스캔의 1등은 정상적인 차익 기회가 아닐 확률이 높다. 대표적으로

- **티커 충돌** — 티커는 같은데 서로 다른 프로젝트. 확인된 사례:
  `AI` (업비트·빗썸 = 젠신 Gensyn, 바이낸스 = Sleepless AI — 가짜 김프 +20%대),
  `PROS` (국내 = Pharos, 바이낸스 = Prosper — 가격이 10배 차이)
- **저유동성** — 최우선 호가에 몇십만원어치밖에 없어 그 수익률로 체결 불가
- **입출금 중단** — 코인을 옮길 수 없어 가격이 따로 노는 상태
  (예: 빗썸 STORJ 입금 중단 중 김프 +37% 로 표시된 실측 사례)

그래서 세 가지 장치가 있다.

1. **`suspicious` 플래그** — 프리미엄이 `SCAN_SUSPICIOUS_PERCENT`(기본 5%)를
   넘으면 표시하고 `suspicion_reason` 에 사유를 담는다.
2. **`min_liquidity_krw` 필터** — 최우선 호가 체결 가능 금액으로 거른다.
3. **`SCAN_EXCLUDED_BASES` 설정** — 티커 충돌이 확인된 코인은 스캔에서 아예 제외.

```bash
# 기본 (필터 없음)
curl "http://localhost:8000/premium/scan"

# 실전용 — 유동성 1,000만원 이상만, 상위 5개
curl "http://localhost:8000/premium/scan?min_liquidity_krw=10000000&limit=5"

# 티커 충돌 코인 제외 (환경변수 — .env 에 넣는 것을 권장)
SCAN_EXCLUDED_BASES='["AI","PROS"]' uvicorn app.main:app
```

#### 응답 `200 OK`

```json
{
  "order": "asc",
  "dom": "upbit",
  "fx_list": ["binance"],
  "usdt_krw_rate": 1406.0,
  "rate_updated_at": 1786370137012,
  "scanned_coins": 198,
  "scanned_pairs": 198,
  "filtered_out": 0,
  "excluded_bases": [],
  "suspicious_count": 2,
  "best_fwd": {
    "sym": "BTC",
    "direction": "fwd",
    "dom": "upbit",
    "dom_price": 91363000.0,
    "fx": "binance",
    "fx_name": "바이낸스",
    "usd": 65034.01,
    "premium_percent": -0.082,
    "premium_krw": -74818.0,
    "liquidity_krw": 34227223.0,
    "suspicious": false,
    "suspicion_reason": null
  },
  "best_rev": { "...": "같은 형태" },
  "top_fwd": [],
  "top_rev": [],
  "data_oldest_at": 1786370137012,
  "data_newest_at": 1786370137012,
  "warnings": ["..."],
  "fetched_at": 1786370950000,
  "elapsed_ms": 21.4
}
```

##### 주요 필드

| 필드 | 설명 |
|---|---|
| `scanned_coins` | 양쪽에 모두 상장되어 실제로 비교된 코인 수 |
| `scanned_pairs` | 비교한 (코인 × 해외 거래소) 조합 수 |
| `filtered_out` | 유동성 필터로 제외된 조합 수 |
| `excluded_bases` | 설정으로 제외한 코인 |
| `dom_price` / `usd` | 국내가 (KRW) 와 해외가 (USDT). 김프면 국내 bid·해외 ask, 역김프면 반대 |
| `liquidity_krw` | 최우선 호가 체결 가능 금액 (원화). 매수·매도 양쪽 중 **작은 쪽** |
| `suspicious` / `suspicion_reason` | **그대로 믿으면 안 되는 값**인지와 그 사유 |

> ⚠️ 최우선 호가 **1단계만** 본다. 금액을 넣었을 때의 실제 수익은 `/arbitrage`
> 또는 `/matrix` 로 확인해야 한다. 수수료·출금 수수료·전송 시간도 미반영이다.

---

### GET /spreads

**(국내 거래소 × 해외 거래소 × 코인)** 페어마다 김프(`fwd`)와 역프(`rev`)를
한 행에 담아 전부 반환한다. **FE 스프레드 탭의 SpreadRow 계약과 1:1** 이다
(`marketlens-fe/src/data/types.ts`). 필터 없이 전체를 반환하고, 거래소·코인
필터링은 FE 가 담당한다.

수익률 계산식은 `/premium/fwd` · `/premium/rev` 와 **완전히 동일**하다 —
체결되는 쪽 호가(살 때 ask, 팔 때 bid)를 쓰고, 각 행은 그 국내 거래소의
자기 환율로 계산한다.

```bash
curl "http://localhost:8000/spreads"
```

#### 응답 `200 OK`

```json
{
  "rate": 1406.0,
  "rows": [
    {
      "sym": "BTC",
      "dom": "upbit",
      "fx": "binance",
      "fwd": 0.53,
      "rev": -0.72,
      "usd": 64950.3,
      "spark": [],
      "status": "ok",
      "age": 4.2,
      "liqDom": 2140.25,
      "liqFx": 2141.78
    }
  ],
  "fetched_at": 1786370950000,
  "elapsed_ms": 3.1
}
```

| 필드 | 설명 |
|---|---|
| `rate` | 기준 USDT/KRW 환율 (기준 국내 거래소 저장값). `usd × rate` 로 원화 환산 |
| `fwd` / `rev` | 순방향 김프 / 역방향 (%). `status=fail` 이면 0 |
| `usd` | 해외 USD(T) 마지막 체결가. `status=fail` 이면 0 |
| `spark` | 프리미엄 추이 스파크라인. **이력 저장 전까지는 항상 빈 배열** |
| `status` | `ok` / `stale`(갱신 후 `SPREAD_STALE_SECONDS`(기본 30초) 초과) / `fail`(저장 호가가 비어 계산 불가) |
| `age` | 스냅샷 마지막 갱신 후 경과 초 (양측 중 오래된 쪽) |
| `liqDom` / `liqFx` | 최우선 호가 유동성 (USD 환산) — 매수·매도 양쪽 중 **작은 쪽**. FE 슬리피지 추정용 |

- 한쪽에만 상장된 코인은 페어가 아니므로 빠진다. `SCAN_EXCLUDED_BASES` 도 적용.
- 스냅샷이나 환율이 하나도 없으면 `404 market_data_not_found`.

---

### GET /slippage/{exchange_id}

거래소 한 곳에서 **시장가로 거래하면 평균 체결가가 얼마나 나빠지는지** 계산한다.
DB 에 저장된 호가 스냅샷만 읽는다.

```
슬리피지(%) = (평균 체결가 - 최우선 호가) / 최우선 호가 × 100
```

매수·매도 모두 **항상 0 이상**이다 (나에게 불리해진 정도). 한 호가 단계에는
정해진 잔량만 있어서, 그보다 많이 거래하면 다음 단계로 파고들며 가격이
불리해진다. **최우선 호가 1단계 안에서 끝나면 슬리피지는 0** 이다.

#### 요청

| 파라미터 | 위치 | 타입 | 필수 | 기본값 | 설명 |
|---|---|---|:---:|---|---|
| `exchange_id` | path | string | ✅ | — | `upbit` \| `bithumb` \| `binance` |
| `symbol` | query | string | ✅ | — | 통일 심볼. QUOTE 는 저장 마켓과 일치해야 한다 |
| `side` | query | enum | | `buy` | `buy`=매도호가를 훑음 / `sell`=매수호가를 훑음 |
| `amount` | query | float (>0) | △ | — | **금액** 기준. 그 마켓의 결제 통화 그대로 (KRW 마켓=원, USDT 마켓=USDT) |
| `quantity` | query | float (>0) | △ | — | **코인 수량** 기준 |
| `depth` | query | int (1–1000) | | `100` | 훑을 호가 단계 수. 저장된 단계 수를 넘으면 있는 만큼만 |

△ `amount` 와 `quantity` 중 **정확히 하나**를 지정해야 한다. 둘 다 주거나 둘 다 안 주면 `400`.

```bash
# 1억원어치 사면?
curl "http://localhost:8000/slippage/upbit?symbol=BTC/KRW&side=buy&amount=100000000"

# 0.5 BTC 팔면?
curl "http://localhost:8000/slippage/upbit?symbol=BTC/KRW&side=sell&quantity=0.5"

# 바이낸스 (USDT 마켓이므로 amount 도 USDT)
curl "http://localhost:8000/slippage/binance?symbol=BTC/USDT&amount=50000"
```

#### 응답 `200 OK`

```json
{
  "exchange": "upbit",
  "name": "업비트",
  "symbol": "BTC/KRW",
  "quote_currency": "KRW",
  "side": "buy",
  "requested_amount": 100000000.0,
  "requested_quantity": null,
  "best_price": 91445000.0,
  "average_price": 91447026.0,
  "worst_price": 91454000.0,
  "quantity": 1.093534,
  "amount": 100000000.0,
  "slippage_percent": 0.0022,
  "slippage_cost": 2216.0,
  "levels_consumed": 4,
  "depth_exhausted": false,
  "depth_available": 30,
  "top_level_amount": 41508816.0,
  "fills": [
    { "level": 1, "price": 91445000.0, "size": 0.453921,
      "amount": 41508816.0, "cumulative_quantity": 0.453921,
      "cumulative_amount": 41508816.0, "cumulative_average": 91445000.0 },
    { "level": 2, "price": 91450000.0, "size": 0.121075,
      "amount": 11072350.0, "cumulative_quantity": 0.574997,
      "cumulative_amount": 52581166.0, "cumulative_average": 91446053.0 }
  ],
  "data_updated_at": 1786370137012,
  "warnings": []
}
```

##### 주요 필드

| 필드 | 설명 |
|---|---|
| `best_price` | 최우선 호가. 매수면 최저 매도호가, 매도면 최고 매수호가 |
| `average_price` | **실제 평균 체결가** |
| `worst_price` | 마지막으로 체결된 단계의 호가 |
| `slippage_percent` | 최우선 호가 대비 불리해진 정도. **항상 0 이상** |
| `slippage_cost` | 슬리피지로 인한 손해액 (결제 통화) |
| `top_level_amount` | **1단계에서 체결 가능한 금액. 이 이하로 거래하면 슬리피지 0** |
| `levels_consumed` / `depth_exhausted` / `depth_available` | 소진 단계 수 / 호가 부족 여부 / 저장된 단계 수 |
| `fills` | 단계별 체결 내역 |
| `data_updated_at` | 이 계산에 쓴 스냅샷의 DB 갱신 시각 — 오래됐으면 `POST /refresh` |

##### `fills` — 업비트 호가창 툴팁과 같은 값

업비트에서 호가에 마우스를 올리면 뜨는 **평균가 · 누적량 · 누적액**이 그대로 들어 있다.

| 툴팁 | `fills[]` 필드 |
|---|---|
| 평균가 | `cumulative_average` |
| 누적량 | `cumulative_quantity` |
| 누적액 | `cumulative_amount` |

검산식도 같다: `cumulative_average × cumulative_quantity = cumulative_amount`

#### 다른 엔드포인트와의 관계

| | 금액 입력 | 호가 훑기 | 거래소 |
|---|:---:|:---:|---|
| `/premium/*` | ❌ | 한 점 (최우선 호가) | 국내 + 해외 |
| **`/slippage/{id}`** | ✅ | ✅ | **한 곳** |
| `/arbitrage` | ✅ | ✅ | 두 곳 (매수처 + 매도처) |
| `/matrix` | ✅ | ✅ | 전 조합 |

> ⚠️ **저장된 스냅샷 호가 기준**이다. 주문 제출과 체결 사이의 가격 변동
> (타이밍 슬리피지)은 반영되지 않는다. 실전 슬리피지는 이 값보다 크다.

---

### GET /matrix

**국내(업비트·빗썸)와 해외(바이낸스)에 모두 상장된 모든 코인**에 대해
한 행씩 반환한다. 각 행에는:

- **가장 큰 김프** 조합 — 구매처 · 판매처 · 표면 프리미엄 · 슬리피지 ·
  구매처 출금 가능 여부 · 판매처 입금 가능 여부
- **가장 큰 역프** 조합 — 위와 동일 (구매처·판매처는 김프와 다를 수 있다)

김프와 역프는 **서로 다른 거래**이므로 최적 조합을 방향마다 따로 고른다.
예: 김프 1등이 (바이낸스 매수 → 업비트 매도)여도, 역프 1등은
(빗썸 매수 → 바이낸스 매도)일 수 있다.

슬리피지는 `amount_krw` **한 개 금액**에 대해 저장된 호가를 실제로 훑어
계산한다. DB 만 읽으므로 **거래소 호출은 0** 이다.

#### 요청

| 파라미터 | 타입 | 기본값 | 설명 |
|---|---|---|---|
| `amount_krw` | float (>0) | `10000000` | 슬리피지 계산에 쓸 투입 금액 (원화). **단일 값** |

```bash
curl "http://localhost:8000/matrix"
curl "http://localhost:8000/matrix?amount_krw=100000000"
```

저장 호가는 서버 설정 `ORDERBOOK_MAX_AMOUNT_KRW`(기본 10억원) 금액까지
커버하므로, 그보다 큰 금액을 넣으면 `depth_exhausted` 가 표시되고 `warnings`
에 안내가 담긴다.

#### 응답 `200 OK`

```json
{
  "amount_krw": 10000000.0,
  "coins": [
    {
      "sym": "XRP",
      "fwd": {
        "buy_exchange": "binance",
        "sell_exchange": "upbit",
        "premium_percent": 0.42,
        "total_slippage_percent": 0.11,
        "withdrawal_available": true,
        "deposit_available": true,
        "depth_exhausted": false
      },
      "rev": {
        "buy_exchange": "bithumb",
        "sell_exchange": "binance",
        "premium_percent": -0.18,
        "total_slippage_percent": 0.09,
        "withdrawal_available": true,
        "deposit_available": null,
        "depth_exhausted": false
      },
      "suspicious": false
    }
  ],
  "scanned_coins": 202,
  "scanned_combinations": 391,
  "dom_list": ["bithumb", "upbit"],
  "fx_list": ["binance"],
  "data_oldest_at": 1786370137012,
  "data_newest_at": 1786370137012,
  "warnings": [
    "거래 수수료·출금 수수료·전송 시간은 반영되지 않았습니다."
  ],
  "fetched_at": 1786370950000,
  "elapsed_ms": 118.7
}
```

##### `coins[]` 필드

| 필드 | 설명 |
|---|---|
| `fwd` | **가장 큰 김프** 조합 (해외 매수 → 국내 매도). 계산 가능한 조합이 없으면 null |
| `rev` | **가장 큰 역프** 조합 (국내 매수 → 해외 매도). 없으면 null |
| `suspicious` | 표면 프리미엄이 비정상적으로 커서 (기본 5% 이상) 티커 충돌·입출금 중단이 의심되는지 |

`coins` 는 **김프 표면 프리미엄 내림차순** 정렬이다 (김프 계산 불가는 뒤로).
`SCAN_EXCLUDED_BASES` 에 등록된 코인은 제외된다.

##### `fwd` / `rev` (방향별 최적 조합) 필드

| 필드 | 설명 |
|---|---|
| `buy_exchange` / `sell_exchange` | 구매처 · 판매처 |
| `premium_percent` | **표면 프리미엄** (%) — 구매처 최우선 매도호가와 판매처 최우선 매수호가만 본 값. 금액과 무관. **최적 조합 선정 기준** |
| `total_slippage_percent` | 요청 금액만큼 호가를 실제로 훑었을 때 표면 프리미엄에서 깎이는 폭 (%p). 실현 수익률 = `premium_percent` - 이 값 |
| `withdrawal_available` | **구매처에서 이 코인을 출금할 수 있는지.** 코인을 옮기려면 구매처 출금이 열려 있어야 한다. 확인 불가(키 없음 등)면 null |
| `deposit_available` | **판매처에서 이 코인을 입금받을 수 있는지.** 확인 불가면 null |
| `depth_exhausted` | 저장된 호가가 부족해 요청 금액을 다 채우지 못했는지. true 면 슬리피지는 요청 금액이 아니라 **실제 체결 가능한 물량 기준**(단위당 손익)으로 계산된 값이다. 그 물량을 넘는 부분은 이 조합으로 거래할 수 없다 |

> 김프 방향은 해외에서 사서 국내로 옮겨야 하므로 **해외 출금 + 국내 입금**이,
> 역프 방향은 **국내 출금 + 해외 입금**이 열려 있어야 실행 가능하다.
> `withdrawal_available` / `deposit_available` 이 그 조합에 맞게 채워진다.

`depth_exhausted` 의 계산 방식 — 예를 들어 1,000만원어치를 샀는데 판매처의
저장 호가가 300만원어치밖에 못 받아주는 경우, **실제로 팔 수 있는 수량만큼만
사고판 것으로 왕복을 맞춰** 수익률을 계산한다. 팔지 못한 코인을 0원 취급하면
단위당 손익과 무관한 -50% 같은 무의미한 값이 나오기 때문이다. (구현:
`matrix_service._direction` — 매도 소진 시 매수측을 `walk_by_quantity` 로 재계산)

> ⚠️ 거래 수수료·출금 수수료·전송 시간은 반영되지 않았다. 입출금 가능 여부가
> null 인 거래소가 있으면 `warnings` 에 API 키 설정 안내가 담긴다.

---

### GET /arbitrage

**금액을 넣으면 실제로 얼마가 남는지** 계산한다. DB 스냅샷만 읽는다.

`/premium` 이 "지금 가격차가 몇 %인가"를 알려준다면, 이쪽은 **"1억을 넣으면
얼마가 남는가"** 를 알려준다. 둘은 다르다 — 프리미엄은 한 점만 보지만, 실제
시장가 주문은 호가창을 위에서부터 **훑어 내려가며** 체결되기 때문이다.

#### 동작

```
1. 대상 코인의 저장된 호가를 모두 currency 통화로 환산
   (국내 거래소는 자기 KRW-USDT 환율, 그 외는 기준 환율)
2. 최우선 매도호가가 가장 싼 곳   → 매수처
   최우선 매수호가가 가장 비싼 곳 → 매도처
3. 투입 금액만큼 매수처의 asks 를 시장가로 훑음 → 코인 수량
4. 그 수량을 매도처의 bids 에 시장가로 훑음     → 수령액
5. 수령액 - 소요액 = 차익
```

#### 요청

| 파라미터 | 타입 | 필수 | 기본값 | 설명 |
|---|---|:---:|---|---|
| `sym` | string | ✅ | — | 대상 코인 심볼 |
| `amount` | float (>0) | ✅ | — | 투입 금액 |
| `currency` | string | | `KRW` | 투입 금액의 통화이자 호가 환산 기준. `KRW` 또는 `USDT` |
| `exchanges` | string[] | | 전체 | 대상 거래소 ID. 반복 지정 가능. 생략하면 DB 에 스냅샷이 있는 모든 거래소 |
| `direction` | enum | | (자동) | `fwd` \| `rev`. **생략하면 가장 싼 곳↔가장 비싼 곳을 자동 선택** — 가장 유리한 조합일 뿐, 스프레드가 가격차보다 크면 음수 수익도 나온다 (`warnings` 표시). 지정하면 그 방향으로 고정되어 손해(음수)도 그대로 |
| `depth` | int (1–1000) | | `100` | 훑을 호가 단계 수. DB 에는 `ORDERBOOK_MAX_AMOUNT_KRW` 커버 단계까지만 저장돼 있다 |

```bash
# 1,000만원으로 BTC 차익거래 (방향 자동 — 가장 유리한 조합)
curl "http://localhost:8000/arbitrage?sym=BTC&amount=10000000"

# 김프 방향으로 고정 (손해면 음수 그대로)
curl "http://localhost:8000/arbitrage?sym=BTC&amount=10000000&direction=fwd"

# 5,000 USDT 로, 거래소 지정
curl "http://localhost:8000/arbitrage?sym=XRP&amount=5000&currency=USDT&exchanges=upbit&exchanges=binance"
```

#### 응답 `200 OK`

```json
{
  "sym": "XRP",
  "direction": null,
  "input_amount_krw": 7030000.0,
  "usdt_krw_rate": 1406.0,
  "premium_percent": 0.1234,
  "buy": {
    "exchange": "upbit", "name": "업비트",
    "average_price_krw": 1444.0, "amount_krw": 7030000.0,
    "slippage_percent": 0.0, "levels_consumed": 1, "depth_exhausted": false,
    "data_updated_at": 1786370137012
  },
  "sell": {
    "exchange": "binance", "name": "바이낸스",
    "average_price_krw": 1446.49, "amount_krw": 7042177.0,
    "slippage_percent": 0.0, "levels_consumed": 1, "depth_exhausted": false,
    "data_updated_at": 1786370137012
  },
  "quantity": 4868.42,
  "withdrawal_available": true,
  "deposit_available": true,
  "profit_krw": 12177.0,
  "profit_percent": 0.1732,
  "premium_capture_percent": 100.0,
  "candidates": [ "..." ],
  "failures": [],
  "warnings": ["거래 수수료·출금 수수료·코인 전송 시간이 반영되지 않은 이론값입니다."],
  "data_oldest_at": 1786370137012,
  "data_newest_at": 1786370137012,
  "fetched_at": 1786370950000,
  "elapsed_ms": 2.8
}
```

##### 최상위 필드

| 필드 | 설명 |
|---|---|
| `direction` | 고정한 방향. `null` 이면 자동 선택 — 실제 경로는 `buy.exchange` → `sell.exchange` |
| `input_amount_krw` | 투입 금액의 원화 환산 (기준 환율 적용) |
| `usdt_krw_rate` | 기준 환율. 국내 거래소 호가에는 각 거래소 자기 환율을 우선 쓰고 없으면 이 값으로 폴백 |
| `premium_percent` | **표면 프리미엄** — 최우선 호가만 본 가격차 (슬리피지 미반영) |
| `buy` / `sell` | 매수처 / 매도처 체결 시뮬레이션 (전부 원화 환산) |
| `quantity` | 매수처에서 매수된 코인 개수 |
| `withdrawal_available` | **매수처에서 이 코인을 출금할 수 있는지.** false 면 코인을 옮길 수 없어 **이 경로는 실행 불가능**하고 `warnings` 에 표시된다. 확인 불가(키 없음)면 null |
| `deposit_available` | **매도처에서 이 코인을 입금받을 수 있는지.** false 면 실행 불가 + 경고. 확인 불가면 null |
| `profit_krw` / `profit_percent` | 차익 (원화) / 수익률. **슬리피지 반영, 수수료 미반영** |
| `premium_capture_percent` | 표면 프리미엄 중 실제로 실현된 비율. `100` 이면 슬리피지 없음 |
| `candidates` | 비교 대상 거래소들의 최우선 시세, 원화 환산 (싼 곳부터) |
| `warnings` | **반드시 확인할 것.** 호가 소진, 수수료 미반영 등 |

##### `buy` / `sell` 필드

| 필드 | 설명 |
|---|---|
| `average_price_krw` | **실제 평균 체결가** (원화 환산) |
| `amount_krw` | 소요/수령 금액 (원화 환산) |
| `slippage_percent` | 최우선 호가 대비 불리해진 정도. **항상 0 이상** |
| `levels_consumed` / `depth_exhausted` | 소진한 호가 단계 수 / 호가 부족 여부 |
| `data_updated_at` | 이 호가 스냅샷의 DB 저장 시각 |

#### 방향 고정 (`direction`)

| `direction` | 매수처 | 매도처 |
|---|---|---|
| (생략) | 최우선 매도호가가 가장 싼 곳 | 최우선 매수호가가 가장 비싼 곳 |
| `fwd` | **해외**(USDT 마켓) 중 가장 싼 곳 | **국내**(KRW 마켓) |
| `rev` | **국내** | **해외** 중 가장 비싼 곳 |

방향을 지정하면 `exchanges` 는 **해외 거래소** 목록으로 해석되고, 국내 거래소는
방향의 한쪽 축이므로 자동으로 포함된다. 손해가 나면 `warnings` 에 알려준다.

#### 금액이 커지면 어떻게 되나

표면 프리미엄은 금액과 무관하지만, 금액이 커질수록 호가창을 깊이 파고들어
실수익이 나빠진다. **프리미엄이 양수여도 금액이 크면 손해**가 날 수 있다.
저장된 호가를 다 소진하면 `depth_exhausted: true` 가 되고, 그 경우 실제
체결액은 요청 금액보다 작다. 이것이 `/premium` 만 보고 판단하면 안 되는 이유다.

#### 실패 조건

| 상황 | HTTP | 코드 |
|---|---|---|
| `amount` 가 0 이하 | 422 | — |
| `currency` 가 KRW/USDT 가 아님 | 400 | `invalid_request` |
| 금액이 너무 작아 체결 불가 | 400 | `invalid_request` |
| DB 에 스냅샷 / 환율 없음 | 404 | `market_data_not_found` |
| 비교 가능한 거래소가 2곳 미만 | 409 | `no_arbitrage_opportunity` |
| 최저 매수처 = 최고 매도처 (기회 없음) | 409 | `no_arbitrage_opportunity` |

409 는 **에러가 아니라 정상적인 시장 상태**다. `POST /refresh` 후 다시 호출하면
달라질 수 있다.

> ⚠️ **모든 수익 계산은 이론값이다.** 거래 수수료(보통 편도 0.04~0.25%),
> 출금 수수료, 코인 전송 시간(그 사이 가격 변동)을 전혀 반영하지 않는다.
> 프리미엄 0.02% 수준은 수수료만으로도 이미 적자다.

---

## 6. 에러 응답

모든 에러는 동일한 형태를 갖는다.

```json
{
  "error": {
    "code": "market_data_not_found",
    "message": "DB 에 upbit 거래소의 BNB 스냅샷이 없습니다. POST /refresh 로 데이터를 수집했는지, 해당 거래소에 상장된 코인인지 확인하세요.",
    "detail": { "exchange": "upbit", "base": "BNB" }
  }
}
```

| HTTP | `code` | 발생 상황 |
|---|---|---|
| 400 | `invalid_symbol` | 심볼이 `BASE/QUOTE` 형식이 아님 |
| 400 | `invalid_request` | 요청 값이 잘못됨 — 지원하지 않는 통화, `dom` 이 원화 거래소가 아님, `/slippage` 에서 `amount`/`quantity` 둘 다 주거나 둘 다 안 줌 등 |
| 401 | — | `POST /refresh` 에서 서버에 `REFRESH_TOKEN` 이 설정됐는데 `X-Refresh-Token` 헤더가 없거나 틀림 |
| 404 | `unsupported_exchange` | 등록되지 않은 거래소 ID |
| 404 | `market_data_not_found` | **DB 에 요청한 데이터가 없음** — 아직 `POST /refresh` 를 안 했거나, 그 거래소에 상장되지 않은 코인이거나, QUOTE 가 저장 마켓과 다름 |
| 409 | `no_arbitrage_opportunity` | **정상 시장 상태.** 비교 가능한 거래소가 2곳 미만이거나 최저 매수처=최고 매도처 |
| 422 | — | FastAPI 기본 검증 실패 (필수 파라미터 누락, `amount` 음수 등) |

거래소 쪽 문제(`exchange_api_error`, `exchange_timeout`, `market_not_found` 등)는
조회 API 에서는 발생하지 않는다 — 거래소를 부르지 않기 때문이다. 이런 에러는
`POST /refresh` 수집 중에 생기며, HTTP 에러가 아니라 **응답의 `failures[]`
(`error_code` 필드) 와 `warnings[]`** 에 담기고 나머지 수집은 계속된다.

---

## 7. 수집기가 호출하는 원본 거래소 API

`POST /refresh` 한 번에 나가는 호출들이다. 시세·호가는 **모두 인증이 필요 없는
public API** 이고, 입출금 상태만 거래소에 따라 키가 필요하다
([8장](#8-api-키--입출금-상태-조회)).

### 업비트 · 빗썸 (v1 API 형태가 같다)

| 용도 | 엔드포인트 | 비고 |
|---|---|---|
| KRW 마켓 목록 | `GET /v1/market/all` | 전종목 목록 |
| 전종목 호가 | `GET /v1/orderbook?markets=A,B,C,...` | 마켓을 나눠 몇 번에 걸쳐 호출. **마켓당 30단계 전부**를 준다 |
| 전종목 현재가 | `GET /v1/ticker?markets=...` | 마지막 체결가 (`trade_price`) |
| KRW-USDT 환율 | `GET /v1/ticker?markets=KRW-USDT` | 마지막 체결가를 `krw_rates` 에 저장 |

- Base URL: `https://api.upbit.com` / `https://api.bithumb.com`
  (설정 `UPBIT_BASE_URL` / `BITHUMB_BASE_URL` 로 교체 가능)
- 업비트 rate limit: 엔드포인트 그룹당 **초당 10회** (응답 헤더 `Remaining-Req`)
- **API 형태가 같아도 커넥터는 일부러 분리돼 있다** (`upbit.py` / `bithumb.py`
  독립 구현) — 한쪽 API 가 바뀌어도 다른 쪽이 흔들리지 않게.
- 빗썸 특이사항 (커넥터가 보정한다):
  - 호가에 **잔량 0 인 유령 호가**가 섞여 온다 → 파싱에서 걸러낸다.
    걸러내면 실제로 쓸 수 있는 호가는 마켓당 15단계 안팎이다 (업비트는 30단계).
  - `ticker` 의 `trade_timestamp` 가 KST 벽시계 기준이라 **정확히 9시간
    미래**로 온다 → 파싱에서 보정한다 (orderbook 의 `timestamp` 는 정상).

### 바이낸스 (현물)

| 용도 | 엔드포인트 | 비고 |
|---|---|---|
| USDT 전종목 현재가 | `GET /api/v3/ticker/price` | 1회 호출로 전종목. 국내 상장 코인과의 **교집합**을 계산해 depth 대상 결정 |
| 심볼별 호가 | `GET /api/v3/depth?symbol=...&limit=100` | 교집합 코인마다 1회. `limit` 은 설정 `BINANCE_ORDERBOOK_DEPTH` (허용값 5/10/20/50/100/500/1000), 동시 실행은 `REFRESH_CONCURRENCY` (기본 20)로 제한 |

- Base URL: `https://api.binance.com` (설정 `BINANCE_SPOT_BASE_URL`)
- rate limit: **분당 6,000 weight** (IP 기준). `depth limit=100` 은 호출당
  weight 5 — 교집합 200종목이면 refresh 1회에 약 1,000 weight 를 쓴다.
  refresh 를 분당 수 회 이상 돌리지 않는 것이 안전하다.

### 입출금 상태

| 거래소 | 엔드포인트 | 인증 |
|---|---|---|
| 업비트 | `GET /v1/status/wallet` | JWT (HS256) — `UPBIT_API_KEY` / `UPBIT_SECRET_KEY` |
| 바이낸스 | `GET /sapi/v1/capital/config/getall` | HMAC-SHA256 — `BINANCE_API_KEY` / `BINANCE_SECRET_KEY` |
| 빗썸 | `GET /public/assetsstatus/ALL` | **불필요 (public)** |

---

## 8. API 키 · 입출금 상태 조회

**시세·호가 수집은 전부 public API 라 키가 없어도 동작한다.** 키가 필요한 곳은
`POST /refresh` 의 **입출금 가능 여부 조회**(업비트·바이낸스)뿐이다.

| 상황 | 결과 |
|---|---|
| 키 있음 | `deposit_enabled` / `withdrawal_enabled` 가 true/false 로 저장 |
| 키 없음 / 조회 실패 | 해당 거래소의 두 필드가 **null** 로 저장, `warnings` 에 안내. 나머지 수집은 정상 |

빗썸은 public 엔드포인트로 조회하므로 키가 필요 없다.

한 코인이 여러 네트워크를 갖는 경우(예: USDT 의 TRX/ERC20), **하나라도 열려
있으면 가능**으로 판정한다.

### 왜 이게 중요한가

김프가 아무리 높아도 **출금이 막혀 있으면 차익거래가 불가능하다.**

```
바이낸스에서 XRP 매수 → 업비트로 전송 → 매도
                          ↑
              여기서 바이낸스 XRP 출금이 정지돼 있으면 끝
```

거래소는 네트워크 혼잡·지갑 점검·상장폐지 예정 등의 이유로 수시로 입출금을
막는다. 특히 **김프가 크게 벌어질 때 국내 거래소가 입금을 막는 경우**가 있어,
프리미엄 숫자만 보고 판단하면 안 된다. `/matrix` 의
`withdrawal_available` / `deposit_available` 이 이 값을 조합 방향에 맞게 보여준다.

### 키 설정

`.env` 에 넣는다 (`.env` 는 반드시 커밋 제외 — `.env.example` 참고):

```bash
# 업비트: Open API 관리에서 발급. 자산조회 권한만 있으면 된다.
UPBIT_API_KEY=...
UPBIT_SECRET_KEY=...
# 바이낸스: API Management 에서 발급. "Enable Reading" 권한만 있으면 된다.
BINANCE_API_KEY=...
BINANCE_SECRET_KEY=...
```

> ⚠️ **출금 권한은 필요 없다.** 조회 전용이므로 키를 만들 때
> 출금(Enable Withdrawals)은 반드시 꺼두는 것이 안전하다.

> ⚠️ 업비트는 API 키 발급 시 **호출할 서버의 공인 IP 를 등록**해야 한다.
> 로컬에서 되던 것이 배포 후 안 되는 흔한 원인이다.

---

## 9. 새 거래소 추가하기 (자동 등록)

**커넥터 파일 하나만 만들면 끝이다. 레지스트리나 라우터는 수정하지 않는다.**

### 1단계: `app/exchanges/connectors/` 에 파일 생성

`BaseExchange` 를 상속한 클래스를 만든다. 기존 `upbit.py` / `bithumb.py` /
`binance.py` 를 참고할 것.

```python
# app/exchanges/connectors/okx.py
from typing import ClassVar

from app.exchanges.base import BaseExchange


class Okx(BaseExchange):
    id: ClassVar[str] = "okx"
    name: ClassVar[str] = "OKX"
    quote_currencies: ClassVar[frozenset[str]] = frozenset({"USDT"})
    default_quote: ClassVar[str] = "USDT"
    is_domestic: ClassVar[bool] = False   # 원화 거래소면 True

    # to_native_symbol, _request_orderbook, _parse_orderbook 등
    # BaseExchange 의 추상 메서드를 구현한다
```

### 2단계: 없음

서버를 재시작하면 자동으로 등록된다.

```bash
curl "http://localhost:8000/exchanges"    # okx 등장
```

### 동작 원리

`app/exchanges/registry.py` 가 import 시점에 다음을 수행한다.

```
pkgutil.iter_modules()     connectors/ 안의 모듈 이름을 나열
        ↓
importlib.import_module()  각 모듈을 임포트
        ↓
inspect.getmembers()       모듈 안의 클래스를 훑음
        ↓
조건 검사 → {id: 인스턴스} 사전에 등록
```

등록 조건:

| 조건 | 이유 |
|---|---|
| `BaseExchange` 하위 클래스 | 거래소 커넥터만 |
| `BaseExchange` 자기 자신이 아님 | 베이스는 제외 |
| 추상 메서드가 전부 구현됨 | 미완성 중간 클래스 제외 |
| **그 모듈에서 정의된** 클래스 | import 해온 클래스 중복 등록 방지 |
| `id` 클래스 속성이 문자열 | 등록 키가 있어야 함 |

파일명이 `_` 로 시작하면 건너뛴다 (`_helpers.py` 같은 내부 모듈).
서로 다른 두 클래스가 같은 `id` 를 선언하면 **기동 시점에 `RuntimeError`** 로 즉시 실패한다.

### 주의할 점 — 수집 대상이 되려면

등록만 하면 `/exchanges` 에는 나오지만, **`POST /refresh` 의 수집 대상이 되는
조건은 따로 있다.**

- **국내 거래소** — `is_domestic = True` 여야 한다. 수집기가 KRW 전종목
  일괄 조회(`fetch_bulk_orderbooks` / `fetch_bulk_quotes`)와 KRW-USDT 환율
  수집 대상에 자동으로 포함한다.
- **해외 거래소** — 현재 수집기는 바이낸스만 해외 수집 경로로 다룬다. 새 해외
  거래소를 수집하려면 `collector_service` 의 해외 수집 단계 확장이 필요하다.
- 입출금 상태를 채우려면 `wallet_status.py` 에 조회 함수를 추가하고
  `collector_service._WALLET_FETCHERS` 에 등록한다.

### 개발 중 재스캔

서버를 재시작하지 않고 다시 스캔하려면:

```python
from app.exchanges import reload
reload()   # -> ['binance', 'bithumb', 'upbit']
```
