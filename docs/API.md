# MarketLens Backend API 문서

거래소 공개 REST API 를 직접 호출해 **현재 호가(orderbook)** 를 수집하고,
거래소 간 가격차와 **김치 프리미엄**을 계산하는 백엔드.

- Base URL (로컬): `http://localhost:8000`
- 인증: **없음** — 모든 엔드포인트가 인증 없이 호출 가능하다.
- 응답 형식: `application/json`
- 대화형 문서: `http://localhost:8000/docs` (Swagger UI), `http://localhost:8000/redoc`

---

## 목차

1. [무엇을 조회할 수 있나 (한눈에 보기)](#1-무엇을-조회할-수-있나-한눈에-보기)
2. [빠른 시작](#2-빠른-시작)
3. [핵심 개념](#3-핵심-개념)
4. [엔드포인트](#4-엔드포인트)
   - [GET /health](#get-health)
   - [GET /exchanges](#get-exchanges)
   - [GET /orderbook/{exchange_id}](#get-orderbookexchange_id)
   - [GET /compare](#get-compare)
   - [GET /premium](#get-premium)
   - [GET /arbitrage](#get-arbitrage)
5. [에러 응답](#5-에러-응답)
6. [원본(raw) 거래소 API 주소](#6-원본raw-거래소-api-주소)
7. [호출 제한 (Rate Limit)](#7-호출-제한-rate-limit)
8. [API 키가 필요한 경우 · 입출금 상태 조회](#8-api-키가-필요한-경우--입출금-상태-조회)
9. [성능](#9-성능)
10. [새 거래소 추가하기 (자동 등록)](#10-새-거래소-추가하기-자동-등록)

---

## 1. 무엇을 조회할 수 있나 (한눈에 보기)

### 엔드포인트별 요약

| 엔드포인트 | 무엇을 얻나 | 입력 | 대표 출력 |
|---|---|---|---|
| `GET /health` | 서비스 생존 여부 | 없음 | `status`, `version` |
| `GET /exchanges` | **지원 거래소 목록과 각 거래소의 능력** | 없음 | 거래소 ID, 이름, 결제 통화, 현물/선물 지원 |
| `GET /orderbook/{id}` | **거래소 한 곳의 호가창 원본** | 거래소, 심볼, 깊이, 시장구분 | 매수/매도 호가 배열, 거래소 타임스탬프, 지연시간 |
| `GET /compare` | **여러 거래소 가격을 한 통화로 환산해 비교** | 코인, 거래소들, 기준통화, 시장구분 | 거래소별 시세 + 최저 매수처/최고 매도처 스프레드 |
| `GET /premium` | **원화 가격이 해외보다 몇 % 비싼가 (김프)** | 코인, 거래소들, 시장구분, **가격기준** | 거래소별 프리미엄 %, 절대 가격차, 적용 환율 |
| `GET /arbitrage` | **N원 넣으면 실제로 얼마 남나** | 코인, **금액**, 통화, 거래소들 | 싼 곳/비싼 곳, 매수 수량, 슬리피지, 실수익 |

### 조회 가능한 데이터 범위

| 항목 | 가능 여부 | 비고 |
|---|:---:|---|
| 실시간 호가 (매수·매도 다단계) | ✅ | 업비트 최대 30단계, 바이낸스 최대 1000단계 |
| **마지막 체결가 (현재가)** | ✅ | `/premium` 의 기본 기준 (`price_basis=last`) |
| 최우선 호가 / 중간가 / 스프레드 | ✅ | `/compare`, `/premium` 응답에 포함 |
| 현물 시세 | ✅ | 업비트, 바이낸스 |
| 선물 시세 | ✅ | 바이낸스만 (업비트는 선물 시장 없음) |
| USDT/KRW 환율 | ✅ | 업비트 `KRW-USDT` (체결가 또는 중간가) |
| 김치 프리미엄 | ✅ | `/premium` |
| 거래소 간 차익 스프레드 | ✅ | `/compare` 의 `spread` (최우선 호가 1단계 기준) |
| **금액 기준 실제 체결 시뮬레이션** | ✅ | `/arbitrage` — 호가창을 실제로 소진시켜 계산 |
| **슬리피지** | ✅ | `/arbitrage` |
| 거래소별 호출 지연시간 | ✅ | 모든 응답에 `latency_ms` |
| 시가·고가·저가·등락률·거래량 | ❌ | **의도적으로 제외** — 아래 참고 |
| 개별 체결 내역 (체결 하나하나) / 캔들 | ❌ | 미구현 |
| 거래·출금 수수료 반영 | ❌ | 모든 수익 계산이 **수수료 미반영 이론값** |
| 잔고 · 주문 · 입출금 | ❌ | 미구현 (API 키 필요) |
| 은행 고시 USD/KRW 기준 프리미엄 | ❌ | 현재는 USDT 기준만 |

### 코인·마켓 범위

| 거래소 | 결제 통화 | 시장 |
|---|---|---|
| 업비트 (`upbit`) | KRW, BTC, USDT | 현물 |
| 바이낸스 (`binance`) | USDT, USDC, BTC, ETH, BNB, FDUSD | 현물, 선물 |

각 거래소에 실제로 상장된 코인이면 무엇이든 조회 가능하다. 상장 여부는 거래소가
`404` 로 알려주므로 별도 목록 관리는 하지 않는다.

---

## 2. 빠른 시작

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

```bash
curl "http://localhost:8000/orderbook/upbit?symbol=BTC/KRW&depth=5"
```

```bash
curl "http://localhost:8000/premium?base=BTC"
```

---

## 3. 핵심 개념

### 통일 심볼 (Unified Symbol)

거래소마다 심볼 표기가 다르다. 이 API 는 **`BASE/QUOTE`** 한 가지 형식만 받고,
내부에서 각 거래소의 네이티브 심볼로 변환한다.

| 요청 심볼 | 업비트 변환 결과 | 바이낸스 변환 결과 |
|---|---|---|
| `BTC/KRW` | `KRW-BTC` | (미지원 — 바이낸스에 KRW 마켓 없음) |
| `BTC/USDT` | `USDT-BTC` | `BTCUSDT` |
| `ETH/USDT` | `USDT-ETH` | `ETHUSDT` |

구분자는 `/`, `-`, `_` 를 모두 허용하며 대소문자를 가리지 않는다.
(`btc-krw`, `BTC_KRW`, `BTC/KRW` 모두 동일하게 처리)

### 매수호가 / 매도호가

| 필드 | 의미 |
|---|---|
| `bids` | 매수 호가. 가격 **내림차순**. `bids[0]` = 최우선 매수호가 → **내가 지금 팔면 받는 가격** |
| `asks` | 매도 호가. 가격 **오름차순**. `asks[0]` = 최우선 매도호가 → **내가 지금 사면 내는 가격** |

즉 차익거래는 `asks[0]` 이 가장 싼 거래소에서 사서, `bids[0]` 이 가장 비싼 거래소에서 판다.

### 기간 요약을 제외한 이유

시가·고가·저가·등락률·거래량 같은 **기간 요약은 제공하지 않는다.**
거래소마다 집계 구간이 달라 같은 이름의 필드에 다른 의미가 섞이기 때문이다.

| 거래소 | 시가·고가·저가·등락률의 구간 |
|---|---|
| 바이낸스 | **롤링 24시간** — 지금부터 정확히 24시간 전까지 |
| 업비트 | **00:00 UTC(= 09:00 KST) 부터 지금까지** |

분봉으로 직접 계산해 확인한 값이다 (KRW-BTC, 13:17 KST):

| 후보 구간 | 고가 | 시가 | 업비트 응답과 일치 |
|---|---|---|---|
| 롤링 24시간 | 92,009,000 | 91,368,000 | |
| KST 자정~ | 92,000,000 | 91,858,000 | |
| **UTC 자정(09:00 KST)~** | **91,343,000** | **91,248,000** | ✅ |

업비트는 한국 거래소지만 날짜 경계가 **KST 자정이 아니라 09:00 KST** 다.
그래서 업비트 구간은 09:00에 0시간으로 시작해 다음날 09:00까지 자란다.

- 09:05 KST에 호출 → 업비트 **5분** vs 바이낸스 **24시간**
- 09:00 KST에 업비트만 리셋되어 등락률이 갑자기 0% 근처로 돌아간다

**지연(시차)이 아니라 측정 구간의 길이 차이다.** 양쪽 다 '지금'까지의 값이지만
시작점이 다르다. 이걸 나란히 놓으면 비교가 성립하지 않으므로 아예 담지 않는다.

> 필요해지면 캔들 API로 롤링 24시간을 직접 계산해서 넣으면 된다.
> 거래소당 호출이 하나 늘어나는 대신 두 거래소를 공정하게 비교할 수 있다.

### 환율

업비트는 KRW, 해외 거래소는 USDT 로 가격을 매기므로 그대로는 비교할 수 없다.
`/compare` 와 `/premium` 은 **업비트 `KRW-USDT` 마켓 시세**를 환율로 쓴다.

`/premium` 은 `price_basis` 에 따라 환율도 같은 기준으로 뽑는다
(`last` → 마지막 체결가, `mid` → 호가 중간가). `/compare` 는 항상 중간가를 쓴다.

> 은행 고시 USD/KRW 환율이 아니라 실제 국내 시장에서 거래되는 테더 가격을 쓴다.
> "업비트에서 원화로 사서 해외에서 팔면 실제로 얼마가 남는가" 에 훨씬 가까운 값이다.

환율은 1초 TTL 캐시가 적용되어 매 요청마다 다시 조회하지 않는다.
기준 거래소와 스테이블코인은 설정으로 바꿀 수 있다
(`krw_reference_exchange`, `fx_stablecoin`).

---

## 4. 엔드포인트

### GET /health

서비스 상태 확인.

```bash
curl "http://localhost:8000/health"
```

```json
{ "status": "ok", "version": "0.1.0" }
```

---

### GET /exchanges

지원 거래소 목록과 각 거래소의 지원 범위.
**커넥터 파일을 추가하면 이 목록에 자동으로 나타난다.**

```bash
curl "http://localhost:8000/exchanges"
```

```json
[
  {
    "id": "upbit",
    "name": "업비트",
    "default_quote": "KRW",
    "quote_currencies": ["BTC", "KRW", "USDT"],
    "market_types": ["spot"]
  },
  {
    "id": "binance",
    "name": "바이낸스",
    "default_quote": "USDT",
    "quote_currencies": ["BNB", "BTC", "ETH", "FDUSD", "USDC", "USDT"],
    "market_types": ["futures", "spot"]
  }
]
```

| 필드 | 설명 |
|---|---|
| `id` | 다른 엔드포인트에서 거래소를 지정할 때 쓰는 값 |
| `default_quote` | `/compare` 에서 자동 선택되는 결제 통화 |
| `quote_currencies` | 이 거래소에서 사용 가능한 결제 통화 |
| `market_types` | 지원하는 시장 구분 |

---

### GET /orderbook/{exchange_id}

거래소 한 곳의 호가창을 조회한다. **가공하지 않은 원본에 가장 가까운 데이터.**

#### 요청

| 파라미터 | 위치 | 타입 | 필수 | 기본값 | 설명 |
|---|---|---|:---:|---|---|
| `exchange_id` | path | string | ✅ | — | `upbit` \| `binance` |
| `symbol` | query | string | ✅ | — | 통일 심볼 (`BTC/KRW`, `BTC/USDT`) |
| `depth` | query | int (1–100) | | `10` | 조회할 호가 단계 수 |
| `market_type` | query | enum | | `spot` | `spot` \| `futures` (바이낸스만 `futures` 지원) |

#### 예시

```bash
# 업비트 현물
curl "http://localhost:8000/orderbook/upbit?symbol=BTC/KRW&depth=3"

# 바이낸스 현물
curl "http://localhost:8000/orderbook/binance?symbol=BTC/USDT&depth=3"

# 바이낸스 선물
curl "http://localhost:8000/orderbook/binance?symbol=BTC/USDT&depth=3&market_type=futures"
```

#### 응답 `200 OK`

```json
{
  "exchange": "upbit",
  "symbol": "BTC/KRW",
  "native_symbol": "KRW-BTC",
  "market_type": "spot",
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
  "latency_ms": 15.71
}
```

| 필드 | 타입 | 설명 |
|---|---|---|
| `exchange` | string | 거래소 ID |
| `symbol` | string | 요청한 통일 심볼 |
| `native_symbol` | string | 실제 거래소에 보낸 심볼 (디버깅용) |
| `market_type` | string | `spot` \| `futures` |
| `base` / `quote` | string | 기준 통화 / 결제 통화 |
| `bids` / `asks` | array | `{price, size}` 배열. `price` 는 quote 통화, `size` 는 base 통화 |
| `timestamp` | int | 거래소 기준 호가 시각 (epoch **밀리초**) |
| `latency_ms` | float | 이 요청이 거래소에서 응답받기까지 걸린 시간 |

> **`timestamp` 주의**: 업비트와 바이낸스 선물은 거래소가 내려준 시각을 그대로 쓴다.
> 바이낸스 **현물** `depth` 응답에는 시각 필드가 없어서 서버 수신 시각으로 대체한다.

---

### GET /compare

여러 거래소의 같은 코인 가격을 하나의 통화로 환산해 비교하고, **차익 스프레드**를 계산한다.

각 거래소의 마켓은 `default_quote` 기준으로 자동 선택되므로,
호출하는 쪽은 "업비트는 KRW, 바이낸스는 USDT" 라는 사실을 몰라도 된다.

#### 요청

| 파라미터 | 타입 | 필수 | 기본값 | 설명 |
|---|---|:---:|---|---|
| `base` | string | ✅ | — | 비교할 코인 (`BTC`, `ETH`, `XRP` …) |
| `exchanges` | string[] | | 전체 | 비교할 거래소 ID. 반복 지정 (`&exchanges=upbit&exchanges=binance`) |
| `common_currency` | string | | `KRW` | 환산 기준 통화. `KRW` 또는 `USDT` |
| `market_type` | enum | | `spot` | `spot` \| `futures` |

#### 예시

```bash
curl "http://localhost:8000/compare?base=BTC"
curl "http://localhost:8000/compare?base=ETH&common_currency=USDT"
curl "http://localhost:8000/compare?base=XRP&exchanges=upbit&exchanges=binance"
```

#### 응답 `200 OK`

```json
{
  "base": "BTC",
  "common_currency": "KRW",
  "usdt_krw_rate": 1421.5,
  "fx_source": "upbit:KRW-USDT (mid price)",
  "quotes": [
    {
      "exchange": "binance",
      "symbol": "BTC/USDT",
      "native_symbol": "BTCUSDT",
      "market_type": "spot",
      "quote_currency": "USDT",
      "best_bid": 64398.0,
      "best_ask": 64398.01,
      "mid_price": 64398.005,
      "best_bid_converted": 91541757.0,
      "best_ask_converted": 91541771.215,
      "mid_price_converted": 91541764.1075,
      "timestamp": 1785941499313,
      "latency_ms": 55.79
    }
  ],
  "failures": [],
  "spread": {
    "buy_exchange": "binance",
    "buy_price": 91541771.215,
    "sell_exchange": "upbit",
    "sell_price": 91553000.0,
    "absolute": 11228.785,
    "percent": 0.0122662964
  },
  "fetched_at": 1785941499313,
  "elapsed_ms": 56.79
}
```

| 필드 | 설명 |
|---|---|
| `usdt_krw_rate` | 환산에 사용한 USDT/KRW 환율 |
| `fx_source` | 환율 출처 |
| `quotes` | 거래소별 시세. **환산가 기준 오름차순**(= 싼 거래소가 먼저) 정렬 |
| `quotes[].best_bid` / `best_ask` / `mid_price` | 해당 거래소의 **원래 통화** 가격 |
| `quotes[].*_converted` | `common_currency` 로 환산한 가격 |
| `failures` | 조회 실패한 거래소. 일부가 실패해도 200 으로 나머지를 반환한다 |
| `spread` | 최저 매수처 ↔ 최고 매도처 차이 |

##### `spread` 필드

| 필드 | 설명 |
|---|---|
| `buy_exchange` / `buy_price` | `best_ask` 가 가장 싼 거래소와 그 가격 |
| `sell_exchange` / `sell_price` | `best_bid` 가 가장 비싼 거래소와 그 가격 |
| `absolute` | `sell_price - buy_price` |
| `percent` | `buy_price` 대비 수익률 (%) |

> ⚠️ **`spread` 는 이론값이다.** 거래 수수료, 출금 수수료, 코인 전송 시간,
> 호가 잔량을 전혀 반영하지 않는다.

> `spread` 가 `null` 인 경우: 비교 가능한 거래소가 2곳 미만이거나,
> 최저 매수처와 최고 매도처가 **같은 거래소**일 때 (= 거래소 간 기회 없음).

---

### GET /premium

**원화 가격이 해외 가격보다 몇 % 비싼지**를 거래소별로 계산한다. 흔히 말하는 김치 프리미엄.

#### 계산식

```
프리미엄 비율 = 원화 가격 / 해외 가격 / 환율
프리미엄 (%)  = (비율 - 1) × 100
```

| 항목 | 값 |
|---|---|
| 원화 가격 | **업비트 KRW 마켓** (고정) |
| 해외 가격 | 대상 거래소의 **USDT 마켓** |
| 환율 | 업비트 `KRW-USDT` |

세 값이 완벽히 맞아떨어지면 비율은 `1.0`, 프리미엄은 `0%` 다.
**양수면 국내가 비싼 것(김프), 음수면 국내가 싼 것(역프)** 이다.

#### 가격 기준 (`price_basis`)

세 가격을 **무엇으로 뽑을지**를 정한다. 셋 다 항상 같은 기준을 쓴다.

| 값 | 사용하는 가격 | 데이터 출처 |
|---|---|---|
| **`last`** (기본값) | 마지막 체결가 | `/ticker` 계열 API |
| `mid` | (최우선 매수호가 + 최우선 매도호가) / 2 | `/orderbook` 계열 API |

**`last` 가 기본값인 이유**: 통상적인 김프 정의가 '현재가' 기준이고, 현재가란
마지막 체결가를 뜻한다. 다른 김프 서비스와 숫자를 맞추려면 이쪽이 맞다.

**`mid` 가 유용한 경우**: 체결이 뜸한 종목은 마지막 체결가가 몇 분 전 값일 수
있다. 그러면 한쪽만 오래된 가격이라 프리미엄이 실제와 다르게 나온다. `mid` 는
호가가 살아있는 한 항상 최신이다.

> **왜 세 가격의 기준을 통일하나**: 마지막 체결가는 그 체결이 매수호가에서 났는지
> 매도호가에서 났는지에 따라 스프레드만큼 차이가 난다. 국내는 체결가, 해외는
> 중간가를 쓰면 그 편향이 그대로 프리미엄에 섞인다. 그래서 셋을 함께 바꾼다.

실제 차이 (BTC, 같은 시각):

| 기준 | 국내 | 해외 | 환율 | 프리미엄 |
|---|---|---|---|---|
| `last` | 90,746,000 | 64,310.01 | 1,412.00 | **-0.0658%** |
| `mid` | 90,739,500 | 64,310.01 | 1,412.50 | **-0.1083%** |

#### 요청

| 파라미터 | 타입 | 필수 | 기본값 | 설명 |
|---|---|:---:|---|---|
| `base` | string | ✅ | — | 조회할 코인 (`BTC`, `ETH`, `XRP` …) |
| `exchanges` | string[] | | 전체 | 비교할 거래소 ID. 반복 지정 가능 |
| `market_type` | enum | | `spot` | 해외 거래소 쪽 시장 구분 |
| `price_basis` | enum | | `last` | `last`(마지막 체결가) \| `mid`(호가 중간값) |

`exchanges` 를 생략하면 **USDT 마켓을 지원하는 모든 거래소에서 업비트를 뺀 나머지**가
대상이 된다. 업비트는 프리미엄의 기준점이므로 자기 자신과 비교하지 않는다.

> 업비트를 **명시적으로 지정**하면 대상에 포함된다. 이 경우 업비트 KRW 마켓과
> 업비트 USDT 마켓을 비교하게 되어, **거래소 내부의 테더 괴리**를 볼 수 있다.

#### 예시

```bash
# 기본 — 지원되는 모든 해외 거래소
curl "http://localhost:8000/premium?base=BTC"

# 특정 거래소만
curl "http://localhost:8000/premium?base=XRP&exchanges=binance"

# 업비트 내부 테더 괴리까지 함께
curl "http://localhost:8000/premium?base=XRP&exchanges=upbit&exchanges=binance"

# 바이낸스 선물 기준
curl "http://localhost:8000/premium?base=BTC&exchanges=binance&market_type=futures"

# 호가 중간가 기준 (체결이 뜸한 종목에 유용)
curl "http://localhost:8000/premium?base=BTC&price_basis=mid"
```

#### 응답 `200 OK`

```json
{
  "base": "XRP",
  "price_basis": "last",
  "krw_exchange": "upbit",
  "krw_symbol": "XRP/KRW",
  "krw_native_symbol": "KRW-XRP",
  "krw_price": 1508.5,
  "krw_timestamp": 1785972565429,
  "usdt_krw_rate": 1418.5,
  "fx_source": "upbit:KRW-USDT (last price)",
  "premiums": [
    {
      "exchange": "binance",
      "name": "바이낸스",
      "symbol": "XRP/USDT",
      "native_symbol": "XRPUSDT",
      "market_type": "spot",
      "quote_currency": "USDT",
      "price": 1.06215,
      "price_in_krw": 1506.66,
      "premium_ratio": 1.0012213938611323,
      "premium_percent": 0.12213938611322916,
      "premium_krw": 1.8402250000001459,
      "timestamp": 1785972565783,
      "latency_ms": 47.63
    }
  ],
  "failures": [],
  "fetched_at": 1785972565784,
  "elapsed_ms": 48.34
}
```

##### 최상위 필드

| 필드 | 설명 |
|---|---|
| `base` | 조회한 코인 |
| `price_basis` | 적용된 가격 기준 (`last` 또는 `mid`) |
| `krw_exchange` / `krw_symbol` / `krw_native_symbol` | 원화 기준이 된 거래소와 마켓 |
| `krw_price` | 원화 기준 가격 (`price_basis` 적용) |
| `krw_timestamp` | 원화 가격 기준 시각 |
| `usdt_krw_rate` | 적용한 USDT/KRW 환율 |
| `fx_source` | 환율 출처 |
| `premiums` | 거래소별 프리미엄. **프리미엄 내림차순**(= 김프가 큰 곳이 먼저) 정렬 |
| `failures` | 조회 실패한 거래소 |
| `elapsed_ms` | 전체 처리 시간 |

##### `premiums[]` 필드

| 필드 | 설명 |
|---|---|
| `exchange` / `name` | 거래소 ID / 이름 |
| `symbol` / `native_symbol` | 통일 심볼 / 거래소 원본 심볼 |
| `price` | 해외 거래소 가격 (USDT 기준, `price_basis` 적용) |
| `price_in_krw` | 위 가격 × 환율 = 원화 환산값 |
| `premium_ratio` | `krw_price / price / 환율`. `1.0` 이면 프리미엄 없음 |
| `premium_percent` | `(비율 - 1) × 100`. **양수 = 김프, 음수 = 역프** |
| `premium_krw` | `krw_price - price_in_krw`. 코인 1개당 원화 차이 |
| `timestamp` / `latency_ms` | 해당 거래소 호가 시각 / 호출 지연시간 |

#### 실패 조건

`/compare` 와 달리 **부분 실패 허용에 예외가 있다.**

| 실패한 것 | 결과 |
|---|---|
| 원화 기준 가격 (업비트 KRW 마켓) | ❌ **전체 실패** — 기준이 없으면 아무것도 계산 못 함 |
| 환율 (업비트 KRW-USDT) | ❌ **전체 실패** — 같은 이유 |
| 개별 해외 거래소 | ✅ `failures` 에 담고 나머지는 정상 반환 |

예를 들어 업비트에 상장되지 않은 코인을 요청하면 `404` 가 난다:

```bash
curl "http://localhost:8000/premium?base=NOTLISTED"
# → 404 market_not_found
```

---

### GET /arbitrage

**금액을 넣으면 실제로 얼마가 남는지** 계산한다.

`/premium` 이 "지금 가격차가 몇 %인가"를 알려준다면, 이쪽은 **"1억을 넣으면 얼마가
남는가"** 를 알려준다. 둘은 다르다 — 프리미엄은 최우선 호가 **한 점**만 보지만,
실제 시장가 주문은 호가창을 위에서부터 **훑어 내려가며** 체결되기 때문이다.

#### 동작

```
1. 대상 거래소 호가를 모두 원화로 환산
2. 최우선 매도호가가 가장 싼 곳   → 매수처
   최우선 매수호가가 가장 비싼 곳 → 매도처
   (프리미엄이 음수면 방향이 자동으로 뒤집힌다)
3. 투입 금액만큼 매수처의 asks 를 시장가로 훑음 → 코인 수량
4. 그 수량을 매도처의 bids 에 시장가로 훑음     → 수령액
5. 수령액 - 소요액 = 차익
```

#### 요청

| 파라미터 | 타입 | 필수 | 기본값 | 설명 |
|---|---|:---:|---|---|
| `base` | string | ✅ | — | 대상 코인 (`BTC`, `XRP` …) |
| `amount` | float (>0) | ✅ | — | 투입 금액 |
| `currency` | string | | `KRW` | 투입 금액의 통화. `KRW` 또는 `USDT` |
| `exchanges` | string[] | | 전체 | 대상 거래소 ID. 반복 지정 가능 |
| `market_type` | enum | | `spot` | `spot` \| `futures` |
| `depth` | int (1–1000) | | `100` | 훑을 호가 단계 수. 업비트는 최대 30단계만 제공 |

#### 예시

```bash
# 1,000만원으로 BTC 차익거래
curl "http://localhost:8000/arbitrage?base=BTC&amount=10000000"

# 5,000 USDT 로, 거래소 지정
curl "http://localhost:8000/arbitrage?base=XRP&amount=5000&currency=USDT&exchanges=upbit&exchanges=binance"
```

#### 응답 `200 OK`

```json
{
  "base": "XRP",
  "market_type": "spot",
  "input_amount": 5000.0,
  "input_currency": "USDT",
  "input_amount_krw": 7065000.0,
  "usdt_krw_rate": 1413.0,
  "fx_source": "upbit:KRW-USDT (last price)",
  "premium_percent": 0.1234,
  "buy": {
    "exchange": "upbit", "name": "업비트", "native_symbol": "KRW-XRP",
    "quote_currency": "KRW",
    "best_price": 1444.0, "average_price": 1444.0, "average_price_krw": 1444.0,
    "amount": 7065000.0, "amount_krw": 7065000.0,
    "slippage_percent": 0.0, "levels_consumed": 1, "depth_exhausted": false,
    "timestamp": 1786077236136, "latency_ms": 54.59
  },
  "sell": {
    "exchange": "binance", "name": "바이낸스", "native_symbol": "XRPUSDT",
    "quote_currency": "USDT",
    "best_price": 1.0232, "average_price": 1.0232, "average_price_krw": 1445.78,
    "amount": 5006.17, "amount_krw": 7073717.0,
    "slippage_percent": 0.0, "levels_consumed": 1, "depth_exhausted": false,
    "timestamp": 1786077236231, "latency_ms": 73.8
  },
  "quantity": 4892.6593,
  "profit_krw": 8717.0,
  "profit_percent": 0.1234,
  "premium_capture_percent": 100.0,
  "candidates": [ ... ],
  "failures": [],
  "warnings": ["거래 수수료·출금 수수료·코인 전송 시간이 반영되지 않은 이론값입니다. ..."],
  "fetched_at": 1786077236231,
  "elapsed_ms": 108.2
}
```

##### 최상위 필드

| 필드 | 설명 |
|---|---|
| `input_amount` / `input_currency` / `input_amount_krw` | 입력 금액과 원화 환산 |
| `premium_percent` | **표면 프리미엄**. 최우선 호가만 본 가격차 (슬리피지 미반영) |
| `buy` / `sell` | 매수처 / 매도처 체결 시뮬레이션 |
| `quantity` | **싼 곳에서 매수된 코인 개수** |
| `profit_krw` | 차익 (원화). `sell.amount_krw - buy.amount_krw` |
| `profit_percent` | 실제 체결액 대비 수익률. **슬리피지 반영, 수수료 미반영** |
| `premium_capture_percent` | 표면 프리미엄 중 실제로 실현된 비율. `100` 이면 슬리피지 없음 |
| `candidates` | 비교 대상 거래소들의 최우선 시세 (싼 곳부터) |
| `warnings` | **반드시 확인할 것.** 호가 소진, 수수료 미반영 등 |

##### `buy` / `sell` 필드

| 필드 | 설명 |
|---|---|
| `best_price` | 최우선 호가 (거래소 원래 통화) |
| `average_price` | **실제 평균 체결가** — 여러 단계를 훑은 결과 |
| `amount` / `amount_krw` | 소요(매수) 또는 수령(매도) 금액 |
| `slippage_percent` | 최우선 호가 대비 얼마나 불리해졌는지. **항상 0 이상** |
| `levels_consumed` | 소진한 호가 단계 수 |
| `depth_exhausted` | 호가가 부족해 요청을 다 채우지 못했는지 |

#### 금액이 커지면 어떻게 되나

같은 시각 BTC 기준 실측이다. 표면 프리미엄은 `0.0256%` 로 고정인데:

| 투입 금액 | 매수 슬리피지 | 매도 슬리피지 | 매수 단계 | 실수익 | 실현율 |
|---|---|---|---|---|---|
| 100만원 | 0.0000% | 0.0000% | 1 | +0.0256% | 100% |
| 1,000만원 | 0.0000% | 0.0000% | 1 | +0.0256% | 100% |
| 1억원 | 0.0187% | 0.0025% | 8 | +0.0045% | **17%** |
| 10억원 | 0.0640% | 0.0082% | 30 | **-0.0465%** | **적자** |

**프리미엄이 양수여도 금액이 크면 손해**가 난다. 10억 구간에서는 업비트 호가 30단계를
모두 소진해 실제로는 4.67억만 체결된다(`depth_exhausted: true`).

이것이 `/premium` 만 보고 판단하면 안 되는 이유다.

#### 실패 조건

| 상황 | HTTP | 코드 |
|---|---|---|
| `amount` 가 0 이하 | 422 | — |
| `currency` 가 KRW/USDT 가 아님 | 400 | `invalid_request` |
| 금액이 너무 작아 체결 불가 | 400 | `invalid_request` |
| 비교 가능한 거래소가 2곳 미만 | 409 | `no_arbitrage_opportunity` |
| 최저 매수처 = 최고 매도처 (기회 없음) | 409 | `no_arbitrage_opportunity` |
| 명시한 거래소가 마켓 미지원 | 400 | `unsupported_market` |

409 는 **에러가 아니라 정상적인 시장 상태**다. 잠시 후 다시 호출하면 달라질 수 있다.

> ⚠️ **모든 수익 계산은 이론값이다.** 거래 수수료(보통 편도 0.04~0.25%),
> 출금 수수료, 코인 전송 시간(그 사이 가격 변동), 호가 잔량의 실시간 변화를
> 전혀 반영하지 않는다. 위 표에서 보듯 프리미엄 0.02% 수준은 수수료만으로도
> 이미 적자다.

---

## 5. 에러 응답

모든 에러는 동일한 형태를 갖는다.

```json
{
  "error": {
    "code": "market_not_found",
    "message": "바이낸스에 NOTACOINUSDT 마켓이 존재하지 않습니다.",
    "detail": { "exchange": "binance", "native_symbol": "NOTACOINUSDT" }
  }
}
```

| HTTP | `code` | 발생 상황 |
|---|---|---|
| 400 | `invalid_symbol` | 심볼이 `BASE/QUOTE` 형식이 아님 |
| 400 | `invalid_request` | 요청 값이 잘못됨 (지원하지 않는 통화, 너무 작은 금액 등) |
| 400 | `unsupported_market` | 해당 거래소가 그 결제 통화 / 시장 구분을 지원하지 않음 |
| 404 | `unsupported_exchange` | 등록되지 않은 거래소 ID |
| 404 | `market_not_found` | 거래소에 그 마켓이 없음 (미상장 코인 등) |
| 409 | `no_arbitrage_opportunity` | **정상 시장 상태.** 비교 가능한 거래소가 2곳 미만이거나 최저 매수처=최고 매도처 |
| 422 | — | FastAPI 기본 검증 실패 (필수 파라미터 누락, `amount` 음수 등) |
| 502 | `exchange_api_error` | 거래소가 에러를 반환하거나 응답을 파싱할 수 없음 |
| 504 | `exchange_timeout` | 거래소 응답 시간 초과 (기본 3초) |

`detail` 은 코드마다 내용이 다르며, `exchange_api_error` 의 경우 거래소 원본 응답 본문
(`body`, 최대 500자)과 상태 코드를 포함해 원인 추적을 돕는다.

---

## 6. 원본(raw) 거래소 API 주소

이 백엔드가 실제로 호출하는 주소. **모두 인증이 필요 없는 public API 다.**

### 업비트

| 항목 | 값 |
|---|---|
| 메서드 | `GET` |
| URL | `https://api.upbit.com/v1/orderbook` |
| 쿼리 | `markets=KRW-BTC` (쉼표로 구분해 여러 마켓 동시 요청 가능) |
| 인증 | 불필요 |
| Rate limit | 초당 10회 · 응답 헤더 `Remaining-Req` 로 잔여량 확인 |
| 공식 문서 | https://docs.upbit.com/kr/reference/호가-정보-조회 |

```bash
curl "https://api.upbit.com/v1/orderbook?markets=KRW-BTC"
```

```json
[
  {
    "market": "KRW-BTC",
    "timestamp": 1785941195636,
    "total_ask_size": 1.62958803,
    "total_bid_size": 3.81182120,
    "orderbook_units": [
      {
        "ask_price": 91601000, "ask_size": 0.00367337,
        "bid_price": 91600000, "bid_size": 0.13990891
      }
    ]
  }
]
```

특징:
- 배열로 감싸서 내려온다 (`[0]` 을 꺼내야 함).
- **하나의 `orderbook_units` 원소 안에 같은 단계의 매수·매도가 함께** 들어있다.
- 호가 개수를 요청 시 지정할 수 없고 **항상 최대 30단계**를 내려준다.
  → `depth` 는 응답을 받은 뒤 잘라내는 방식으로 적용한다.
- 없는 마켓 요청 시 `404` + `{"error":{"name":404,"message":"Code not found"}}`.

**이 엔드포인트는 세 가지 용도로 쓰인다:**
1. `/orderbook/upbit` — 사용자가 요청한 호가
2. 환율 (`price_basis=mid` 일 때) — `markets=KRW-USDT` 중간가
3. `/premium` 의 원화 기준가 (`price_basis=mid` 일 때) — `markets=KRW-{코인}`

### 업비트 (마지막 체결)

| 항목 | 값 |
|---|---|
| 메서드 | `GET` |
| URL | `https://api.upbit.com/v1/ticker` |
| 쿼리 | `markets=KRW-BTC` |
| 인증 | 불필요 |
| 공식 문서 | https://docs.upbit.com/kr/reference/ticker현재가-정보 |

```bash
curl "https://api.upbit.com/v1/ticker?markets=KRW-BTC"
```

```json
[{
  "market": "KRW-BTC",
  "trade_price": 90746000.0,
  "trade_timestamp": 1786075786595,
  "opening_price": 91248000.0,
  "high_price": 91343000.0,
  "low_price": 90700000.0,
  "signed_change_rate": -0.0054905915,
  "acc_trade_volume_24h": 491.88958314,
  "acc_trade_price_24h": 44971270224.85124
}]
```

**응답에서 우리가 쓰는 필드는 `trade_price`(마지막 체결가)와
`trade_timestamp`(체결 시각) 둘뿐이다.** 둘 다 진짜 마지막 체결값이다 —
33분간 거래가 없던 `KRW-MOC` 에서 `/v1/trades/ticks` 의 최신 체결과
가격·시각이 정확히 일치함을 확인했다.

같이 오는 `opening_price` / `high_price` / `signed_change_rate` / `acc_trade_*` 같은
기간 요약은 쓰지 않는다. 이유는 [기간 요약을 제외한 이유](#기간-요약을-제외한-이유) 참고.

**이 엔드포인트는 `price_basis=last`(기본값)일 때의 환율과 원화 기준가에 쓰인다.**

### 바이낸스 (현물)

| 항목 | 값 |
|---|---|
| 메서드 | `GET` |
| URL | `https://api.binance.com/api/v3/depth` |
| 쿼리 | `symbol=BTCUSDT&limit=10` |
| 인증 | 불필요 |
| `limit` 허용값 | `5, 10, 20, 50, 100, 500, 1000, 5000` |
| Rate limit | 가중치 기반 (limit 이 클수록 가중치 증가), 분당 6000 weight |
| 공식 문서 | https://developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints |

```bash
curl "https://api.binance.com/api/v3/depth?symbol=BTCUSDT&limit=5"
```

```json
{
  "lastUpdateId": 98263555708,
  "bids": [["64442.62000000", "3.94783000"]],
  "asks": [["64442.63000000", "1.14367000"]]
}
```

### 바이낸스 (선물 / USDⓈ-M)

| 항목 | 값 |
|---|---|
| 메서드 | `GET` |
| URL | `https://fapi.binance.com/fapi/v1/depth` |
| 쿼리 | `symbol=BTCUSDT&limit=10` |
| 인증 | 불필요 |
| `limit` 허용값 | `5, 10, 20, 50, 100, 500, 1000` |
| 공식 문서 | https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data |

특징 (현물과의 차이):
- 응답에 `E` (이벤트 시각), `T` (거래 엔진 시각) 필드가 **있다**. 현물에는 없다.
- `limit` 최대값이 1000 (현물은 5000).
- 없는 심볼 요청 시 `400` + `{"code":-1121,"msg":"Invalid symbol."}`.

### 바이낸스 (마지막 체결)

| 항목 | 값 | |
|---|---|---|
| 현물 | `GET https://api.binance.com/api/v3/aggTrades` | `?symbol=BTCUSDT&limit=1` |
| 선물 | `GET https://fapi.binance.com/fapi/v1/aggTrades` | `?symbol=BTCUSDT&limit=1` |

인증 불필요. 응답에서 `p`(체결가)와 `T`(체결 시각)만 쓴다.

```bash
curl "https://api.binance.com/api/v3/aggTrades?symbol=BTCUSDT&limit=1"
```

```json
[{ "a": 486, "p": "231.95000000", "q": "0.05100000",
   "T": 1786073258565, "m": false, "M": true }]
```

- 응답은 **오래된 순** 배열이므로 마지막 원소가 최신이다.
- 없는 심볼은 depth 와 동일하게 `-1121`.

> **왜 `ticker/24hr` 을 쓰지 않는가**
>
> `GET /api/v3/ticker/24hr` 의 `lastPrice` 는 마지막 체결가가 맞지만,
> `closeTime` 은 공식 문서상 **"End of the ticker interval"** — 즉 통계 윈도우의
> 끝이지 마지막 체결 시각이 아니다.
>
> 실측으로 확인했다. 거래가 한산한 `CRDOBUSDT` 에서:
>
> | | 값 |
> |---|---|
> | `closeTime` | 53초 전 (≈ 현재) |
> | 실제 마지막 체결 (`aggTrades`) | **3,716초 전 (62분)** |
>
> `closeTime` 을 쓰면 62분 전 가격을 방금 체결된 값으로 오인하게 된다.
> 반면 업비트 `trade_timestamp` 는 진짜 마지막 체결 시각이다
> (33분간 거래 없던 `KRW-MOC` 에서 `/v1/trades/ticks` 와 정확히 일치 확인).
> 두 거래소의 의미를 맞추기 위해 바이낸스는 `aggTrades` 를 쓴다.

---

## 7. 호출 제한 (Rate Limit)

이 백엔드 자체에는 제한이 없다. 다만 **뒤에 있는 거래소 API 에 제한이 있고**,
우리 엔드포인트 하나가 거래소를 여러 번 호출하므로 실질 한도는 아래와 같다.

### 거래소별 원본 제한

측정값이다 (응답 헤더와 `exchangeInfo` 에서 직접 확인).

#### 업비트 — **엔드포인트 그룹별 개수 제한**

응답 헤더 `Remaining-Req` 로 잔여량을 알려준다.

```
remaining-req: group=orderbook; min=600; sec=9
```

| 그룹 | 해당 엔드포인트 | 제한 |
|---|---|---|
| `orderbook` | `/v1/orderbook` | **초당 10회 / 분당 600회** |
| `ticker` | `/v1/ticker` | 초당 10회 / 분당 600회 |
| `crix-trades` | `/v1/trades/ticks` | 초당 10회 / 분당 600회 |
| `market` | `/v1/market/all` | 초당 10회 / 분당 600회 |

**그룹마다 할당량이 따로다.** 호가를 초당 10번 부르면서 동시에 티커도 초당 10번
부를 수 있다. 초과하면 `429 Too Many Requests` 가 온다.

#### 바이낸스 — **가중치(weight) 총량 제한**

응답 헤더 `X-MBX-USED-WEIGHT-1M` 으로 사용량 누적치를 알려준다.

| 시장 | 한도 |
|---|---|
| 현물 | **분당 6,000 weight** (IP 기준) |
| 선물 | **분당 2,400 weight** (IP 기준) |

엔드포인트별 가중치 (연속 호출 델타로 실측):

| 엔드포인트 | weight |
|---|---|
| `/api/v3/depth` `limit=1~100` | **5** |
| `/api/v3/depth` `limit=101~500` | 25 |
| `/api/v3/depth` `limit=501~1000` | 50 |
| `/api/v3/aggTrades` | **4** |
| `/api/v3/ticker/24hr` (단일 심볼) | 2 |
| `/api/v3/ticker/price` (단일 심볼) | 2 |

초과하면 `429`, 계속하면 `418` 과 함께 **IP 차단**(최소 2분, 반복 시 최대 3일)된다.

### 우리 엔드포인트별 초당 호출 한도 ⭐

**분당 weight 는 그대로 쓰면 오해를 부른다.** 분당 6,000 weight 여도 한 요청이
50 weight 를 쓰면 분당 120회, 즉 **초당 2회**밖에 안 된다. 그래서 아래 표는
weight 를 전부 초당 지속 가능 호출 수로 환산했다.

```
바이낸스 현물 : 6,000 weight/분 = 100 weight/초
바이낸스 선물 : 2,400 weight/분 =  40 weight/초
업비트        : 그룹당 10회/초 (= 600회/분, 동일한 값)

초당 한도 = min(업비트 그룹 여유분, 거래소 weight 한도 ÷ 요청당 weight)
```

#### 현물 (기본값)

| 엔드포인트 | 업비트 소비 | 바이낸스 weight | **초당 한도** | 병목 |
|---|---|---|---|---|
| `GET /health`, `/exchanges` | — | — | **무제한** | 외부 호출 없음 |
| `GET /orderbook/upbit` | orderbook ×1 | — | **10회/초** | 업비트 |
| `GET /orderbook/binance` (depth≤100) | — | 5 | **20회/초** | 바이낸스 |
| `GET /compare` | orderbook ×1 | 5 | **10회/초** | 업비트 |
| `GET /premium` (`last`, 기본) | ticker ×1 (+환율) | 4 | **9회/초** | 업비트 |
| `GET /premium` (`mid`) | orderbook ×1 (+환율) | 5 | **9회/초** | 업비트 |
| `GET /arbitrage` (`depth≤100`, 기본) | orderbook ×1 | 5 | **10회/초** | 업비트 |
| `GET /arbitrage` (`depth=500`) | orderbook ×1 | 25 | **4회/초** | 바이낸스 |
| `GET /arbitrage` (`depth=1000`) | orderbook ×1 | 50 | **2회/초** | 바이낸스 |

#### 선물 (`market_type=futures`)

선물은 한도가 절반 이하(2,400/분)라 더 빡빡하다.

| 엔드포인트 | 바이낸스 weight | **초당 한도** |
|---|---|---|
| `GET /orderbook/binance` (depth≤50) | 2 | **20회/초** |
| `GET /orderbook/binance` (depth 51~100) | 5 | **8회/초** |
| `GET /arbitrage` (depth≤100) | 5 | **8회/초** |
| `GET /arbitrage` (depth=1000) | 20 | **2회/초** |

#### 왜 `/premium` 만 9회/초인가

환율 조회가 **코인 조회와 같은 그룹**을 쓰기 때문이다.

| 엔드포인트 | 코인 조회 | 환율 조회 | 그룹 충돌 |
|---|---|---|---|
| `/premium` (`last`) | ticker | **ticker** | ⚠️ 같은 그룹 → 초당 1회를 뺏김 |
| `/premium` (`mid`) | orderbook | **orderbook** | ⚠️ 같은 그룹 |
| `/compare` | orderbook | ticker | ✅ 다른 그룹 |
| `/arbitrage` | orderbook | ticker | ✅ 다른 그룹 |

환율은 1초 TTL 캐시라 초당 최대 1회만 나간다. 그래서 `10 - 1 = 9회/초` 다.

#### 지속 vs 순간 (burst)

위 숫자는 **지속 가능한** 속도다. 두 거래소의 한도 방식이 달라서 순간 폭주 시
동작이 다르다.

| | 순간 폭주 시 |
|---|---|
| 업비트 | 초당 카운터라 **11번째 요청이 즉시 `429`**. 1초 뒤 회복 |
| 바이낸스 | 분당 총량이라 **1초에 1,200 weight 를 몰아 쓸 수 있고**, 그 대신 남은 59초 동안 차단 |

바이낸스는 `depth=1000` 을 1초에 120번 부를 수 있지만 그 뒤 59초간 아무것도 못 한다.
평균 2회/초와 결과가 같으므로, 표의 지속 속도를 기준으로 설계하는 게 안전하다.

#### 거래소를 추가하면

`/compare`, `/premium`, `/arbitrage` 는 대상 거래소 수만큼 호출이 늘어난다.
다만 **동시(`asyncio.gather`)로 나가므로 응답 시간은 그대로**이고, 한도만 나뉜다.
새로 추가되는 거래소가 국내 거래소(KRW)라면 업비트와 별개 한도를 가지므로
전체 처리량은 오히려 늘어난다.

### 한도를 늘리려면

1. **캐시 TTL 을 늘린다** — `fx_cache_ttl` (기본 1초). 환율은 초 단위로 크게 변하지
   않으므로 3~5초로 늘려도 무방하다.
2. **호가 응답을 캐싱한다** — 현재는 매번 조회한다. 동일 심볼 요청이 몰리면
   짧은 TTL 캐시가 큰 효과를 낸다.
3. **`depth` 를 줄인다** — 바이낸스 weight 가 `limit=100` 5 → `limit=1000` 50 으로
   10배 뛴다. 큰 금액을 다루지 않는다면 낮게 유지한다.
4. **WebSocket 으로 전환한다** — 연결 1개를 유지하며 푸시받으므로 REST 호출 제한이
   사실상 사라진다. 실시간 감시가 목적이라면 이쪽이 정석이다.

---

## 8. API 키가 필요한 경우 · 입출금 상태 조회

**현재 이 백엔드의 모든 엔드포인트는 API 키 없이 동작한다.** 호가·시세 조회는
두 거래소 모두 public API 로 열려 있기 때문이다.

| 기능 | 업비트 | 바이낸스 |
|---|---|---|
| **호가·시세 조회** | ❌ **불필요** | ❌ **불필요** |
| **입금/출금 가능 여부** | ✅ **필요** | ✅ **필요** |
| 잔고 조회 | ✅ 필요 | ✅ 필요 |
| 주문 생성/취소 | ✅ 필요 | ✅ 필요 |
| 실제 입출금 실행 | ✅ 필요 | ✅ 필요 |

### 입금/출금 가능 여부는 어디서 얻나

**두 거래소 모두 인증이 필요하다.** 무인증으로 호출하면 이렇게 막힌다:

```bash
curl "https://api.upbit.com/v1/status/wallet"
# 401 {"error":{"message":"Please check Authorization Header",...}}

curl "https://api.binance.com/sapi/v1/capital/config/getall"
# 400 {"code":-2014,"msg":"API-key format invalid."}
```

#### 업비트 — `GET /v1/status/wallet`

| 항목 | 값 |
|---|---|
| URL | `https://api.upbit.com/v1/status/wallet` |
| 인증 | **JWT (HS256)** — `Authorization: Bearer <token>` |
| 필요 권한 | 자산 조회 |

응답의 `wallet_state` 하나로 입출금 상태가 결정된다:

| `wallet_state` | 입금 | 출금 |
|---|:---:|:---:|
| `working` | ✅ | ✅ |
| `withdraw_only` | ❌ | ✅ |
| `deposit_only` | ✅ | ❌ |
| `paused` | ❌ | ❌ |
| `unsupported` | ❌ | ❌ |

`net_type`(네트워크)별로 행이 따로 오므로 **코인 + 네트워크 조합마다** 상태가 다르다.
(예: USDT 는 TRX 는 열려 있는데 ETH 는 막혀 있을 수 있다)

#### 바이낸스 — `GET /sapi/v1/capital/config/getall`

| 항목 | 값 |
|---|---|
| URL | `https://api.binance.com/sapi/v1/capital/config/getall` |
| 인증 | **HMAC-SHA256 서명** + `X-MBX-APIKEY` 헤더 |
| 필요 권한 | **`USER_DATA`** — 읽기(Enable Reading) 권한만 있으면 된다 |
| Weight | 10 |

응답 필드:

| 필드 | 의미 |
|---|---|
| `depositAllEnable` / `withdrawAllEnable` | 코인 전체의 입금/출금 가능 여부 |
| `networkList[].depositEnable` | **네트워크별** 입금 가능 여부 |
| `networkList[].withdrawEnable` | **네트워크별** 출금 가능 여부 |
| `networkList[].withdrawFee` | 출금 수수료 |
| `networkList[].withdrawMin` | 최소 출금 수량 |

> **출금 권한은 필요 없다.** `USER_DATA` 는 읽기 전용이므로, 키를 만들 때
> **출금(Enable Withdrawals)은 반드시 꺼두는 것**이 안전하다. 조회만 할 거라면
> 읽기 권한만으로 충분하다.

### 왜 이게 중요한가

김프가 아무리 높아도 **출금이 막혀 있으면 차익거래가 불가능하다.**

```
바이낸스에서 XRP 매수 → 업비트로 전송 → 매도
                          ↑
              여기서 바이낸스 XRP 출금이 정지돼 있으면 끝
```

거래소는 네트워크 혼잡·지갑 점검·상장폐지 예정 등의 이유로 수시로 입출금을
막는다. 특히 **김프가 크게 벌어질 때 국내 거래소가 입금을 막는 경우**가 있어,
`/premium` 이나 `/arbitrage` 결과만 보고 판단하면 안 된다.

### 키를 추가한다면

환경변수로 주입한다 (`.env` 는 반드시 커밋 제외):

```bash
UPBIT_API_KEY=...
UPBIT_SECRET_KEY=...
BINANCE_API_KEY=...
BINANCE_SECRET_KEY=...
```

인증 방식은 서로 다르다:

- **업비트**: JWT(`HS256`). payload 에 `access_key` + `nonce` (+ 파라미터가 있으면
  쿼리스트링의 SHA512 해시인 `query_hash`) 를 담아 secret key 로 서명하고
  `Authorization: Bearer <jwt>` 헤더로 보낸다.
- **바이낸스**: HMAC-SHA256. 쿼리스트링 전체를 secret key 로 서명해 `signature`
  파라미터로 붙이고, `X-MBX-APIKEY` 헤더에 API key 를 담는다.

> ⚠️ 업비트는 API 키 발급 시 **호출할 서버의 공인 IP 를 등록**해야 한다.
> 로컬에서 되던 것이 배포 후 안 되는 흔한 원인이다.

---

## 9. 성능

ccxt 라이브러리 대신 원본 REST 엔드포인트를 직접 호출하는 이유는 속도다.

| 방식 | 중앙값 | 평균 |
|---|---|---|
| ccxt (동기 · 순차 호출) | 108.5 ms | 102.8 ms |
| MarketLens (비동기 · 동시 호출) | **47.5 ms** | **47.7 ms** |

> 업비트 `KRW-BTC` + 바이낸스 `BTCUSDT` 호가를 함께 가져오는 동일 작업, 10회 반복.
> ccxt 쪽에는 `load_markets()` 를 측정 밖에서 미리 끝내주는 유리한 조건을 줬다.

재현:

```bash
pip install ccxt
python -m scripts.benchmark
```

속도 차이의 원인:

1. **동시 호출** — `asyncio.gather` 로 병렬 요청. `/premium` 은 원화 가격·환율·해외
   가격 **세 가지를 동시에** 던진다. (가장 큰 요인)
   거래소가 늘어도 호가/티커 조회는 `fan_out` 으로 전부 동시에 나간다.
2. **커넥션 재사용** — 프로세스 수명 동안 살아있는 단일 `httpx.AsyncClient` 를 공유해
   매 요청의 TLS 핸드셰이크 비용을 제거한다.
3. **불필요한 작업 제거** — 호가에 필요한 필드만 파싱한다.

---

## 10. 새 거래소 추가하기 (자동 등록)

**커넥터 파일 하나만 만들면 끝이다. 레지스트리나 라우터는 수정하지 않는다.**

### 1단계: `app/exchanges/connectors/` 에 파일 생성

```python
# app/exchanges/connectors/bithumb.py
from typing import Any, ClassVar

from app.exchanges.base import BaseExchange
from app.models.orderbook import MarketType, OrderBook, OrderBookLevel
from app.models.symbol import Symbol


class Bithumb(BaseExchange):
    id: ClassVar[str] = "bithumb"
    name: ClassVar[str] = "빗썸"
    quote_currencies: ClassVar[frozenset[str]] = frozenset({"KRW"})
    default_quote: ClassVar[str] = "KRW"

    def to_native_symbol(self, symbol, market_type):
        ...   # BTC/KRW -> BTC_KRW

    async def _request_orderbook(self, native_symbol, depth, market_type):
        ...   # self._get_json(url, params=...) 호출

    def _parse_orderbook(self, raw, *, symbol, native_symbol, market_type, depth, latency_ms):
        ...   # OrderBook 모델로 변환
```

### 2단계: 없음

서버를 재시작하면 자동으로 등록된다.

```bash
curl "http://localhost:8000/exchanges"          # bithumb 등장
curl "http://localhost:8000/orderbook/bithumb?symbol=BTC/KRW"   # 바로 동작
curl "http://localhost:8000/compare?base=BTC"   # 비교 대상에 자동 포함
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

### 주의할 점

- **`/premium` 대상이 되려면 `quote_currencies` 에 `USDT` 가 있어야 한다.**
  KRW 전용 거래소는 `/orderbook` 과 `/compare` 에서만 쓰인다.
- 거래소가 "없는 마켓" 을 어떤 상태 코드/본문으로 알리는지 확인해
  `MarketNotFoundError` 로 매핑해야 404 가 올바르게 나간다.
- `_conversion_factor` 는 KRW/USDT 만 환산한다. 다른 결제 통화를 비교 대상에
  넣으려면 `comparison_service._CONVERTIBLE` 확장이 필요하다.

### 개발 중 재스캔

서버를 재시작하지 않고 다시 스캔하려면:

```python
from app.exchanges import reload
reload()   # -> ['binance', 'bithumb', 'upbit']
```
