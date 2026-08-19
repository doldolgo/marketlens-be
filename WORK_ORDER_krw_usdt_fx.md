# 작업지시서 — 환율 기준을 KRW-USDT 방향별 호가로 전환

작성일 2026-08-19 · 대상 리포 `/Users/jin/Documents/marketlens-be` · 배포 `http://3.34.104.16:8000`

---

## 0. 한 줄 요약

모든 원화 환산 환율을 **하나은행 고시 매매기준율**에서 **국내 거래소 KRW-USDT 호가**로 되돌리되, 예전처럼 체결가(last) 하나를 쓰는 게 아니라 **방향별로 ask1 / bid1을 따로** 쓴다. 환율 소스는 **각 행의 국내(dom) 거래소와 동일한 거래소**를 쓴다.

---

## 1. 배경 — 왜 되돌리나

현재 구현은 커밋 `9ef7963` (PR #7, 2026-08-13 작성 / 2026-08-14 PR #11로 통합 머지)에서 들어왔다. PR 본문에 적힌 근거는 다음 한 줄이다.

> **왜:** KRW-USDT 시세에는 테더 프리미엄이 섞여 거래소마다 다르고 은행 환율 기준 김프와 어긋남

이 판단이 뒤집혔다. 이유:

1. **테더 프리미엄은 노이즈가 아니라 실제 비용이다.** 김프 차익의 자금 경로는 `원화 → USDT 매수 → 해외 송금 → 코인 매수 → 국내 전송 → KRW 매도`다. 원화↔달러 전환은 은행이 아니라 USDT 호가에서 일어난다. 은행 고시 환율로 계산한 김프는 **표시용 지표**이지 실행 수익률이 아니다.
2. **오차가 가장 커지는 시점이 하필 기회가 생기는 시점이다.** 테더 프리미엄이 벌어지는 구간(국내 급등장·상장 펌핑)이 곧 김프가 벌어지는 구간이라, 은행 환율 기준은 정확히 필요할 때 틀린다.
3. **거래소별 차이를 구조적으로 표현할 수 없다.** `9ef7963`이 `krw_rates`(거래소별 환율) 테이블을 지우고 단일 행으로 바꿔서, 업비트 김프와 빗썸 김프가 서로 다른 테더 프리미엄을 반영하지 못한다.
4. PR #7과 #11 모두 **코멘트 0 / 리뷰 0**이다. 제품 핵심 정의가 리뷰 없이 들어갔다.

참고로 이 전환은 **단순 revert가 아니다.** `9ef7963` 이전 구현(`collector_service._krw_rate`)은 KRW-USDT의 **마지막 체결가(last)** 를 환율로 썼다. 지금 요구는 그보다 한 단계 정교한 방향별 ask1/bid1이므로, 옛 코드를 참고하되 새로 구현한다.

---

## 2. 확정된 결정

| # | 항목 | 결정 |
|---|---|---|
| D1 | 환율 소스 | 국내 거래소 **KRW-USDT 호가** |
| D2 | 호가 선택 | **방향별로 따로** — 김프(fwd)는 ask1, 역프(rev)는 bid1 |
| D3 | 어느 거래소 | **각 행의 dom 거래소와 동일** (업비트 김프 → 업비트 USDT, 빗썸 김프 → 빗썸 USDT) |
| D4 | 하나은행 | **전면 폐기.** 라이브·이력 모두 KRW-USDT로 통일하고, 하나은행 기준으로 백필된 `premium_archive`는 버린다 |

---

## 3. 새 계산 규격

### 3.1 공식

```
fwd (김프)  = dom_bid1 / (fx_ask1 × usdt_ask1[dom]) - 1
rev (역프)  = (fx_bid1 × usdt_bid1[dom]) / dom_ask1 - 1
```

`usdt_ask1[dom]` = 그 행의 dom 거래소 KRW-USDT 마켓 **최우선 매도호가**
`usdt_bid1[dom]` = 같은 거래소 KRW-USDT 마켓 **최우선 매수호가**

### 3.2 왜 방향별로 다른 호가인가

| 방향 | 자금 경로 | USDT 거래 | 쓰는 호가 |
|---|---|---|---|
| fwd (김프) | 원화 → **USDT 매수** → 해외 코인 매수 → 국내 KRW 매도 | **산다** | **ask1** |
| rev (역프) | 원화 → 국내 코인 매수 → 해외 매도(USDT 수령) → **USDT 매도** → 원화 | **판다** | **bid1** |

`usdt_ask1 > usdt_bid1`이므로 양방향 모두 현재보다 **보수적으로(낮게)** 나온다. 이는 버그가 아니라 스프레드를 비용으로 인식한 정상 결과다.

### 3.3 기존 원칙과의 일관성

이미 코드 전반이 "체결되는 쪽 호가를 쓴다"(살 때 ask, 팔 때 bid)를 지키고 있다 — `premium_service.resolve_side`, `spread_service._build_row`. **환율만 그 원칙 밖에 있었다.** 이번 작업은 새 규칙 도입이 아니라 기존 규칙을 환율까지 확장하는 것이다.

---

## 4. 손댈 지점

### 4.1 ⭐ 핵심 발견 — KRW-USDT 호가는 이미 수집되고 있고, 버려지는 중이다

`app/services/collector_service.py:620` `_domestic_market`이 이렇게 부른다.

```python
exchange.fetch_bulk_orderbooks(settings.krw_reference_quote, depth=30)  # KRW 전종목
```

USDT도 KRW 마켓 종목이므로 **USDT/KRW 호가는 이미 응답에 들어 있다.** 그런데 `collector_service.py:317`에서 탈락한다.

```python
intersection = domestic_union & overseas_union   # ← 바이낸스에 USDTUSDT 가 없어 USDT 탈락
```

배포본에서 확인 완료:
```
GET /orderbook/upbit?symbol=USDT/KRW
→ {"error":{"code":"market_data_not_found", ...}}
```

**→ 거래소 API 추가 호출 0회로 환율을 얻을 수 있다.** 교집합 필터를 통과시키지 말고(그러면 USDT가 김프 계산 대상 코인으로 잘못 들어간다), **환율 전용 경로로 따로 빼내라.**

### 4.2 파일별 작업 범위

| 파일 | 지금 | 해야 할 일 |
|---|---|---|
| `app/services/collector_service.py:651` `_usdkrw_rate` | `hana.fetch_latest()` 호출 | 삭제. 대신 `_domestic_market` 결과에서 dom별 USDT ask1/bid1 추출 |
| `app/services/collector_service.py:317` 교집합 | USDT 탈락 | 유지. 단 탈락 **전에** USDT 호가를 환율용으로 보관 |
| `app/db/models.py:114` `UsdKrwRate` | 단일 행(`id=1`), `rate: float`, `source_time`, `round_no` | 거래소별 행 + `ask`/`bid` 두 값으로 재설계 (`round_no`·`source_time`은 은행 개념이라 제거) |
| `app/db/repository.py:139,596,601` | `upsert_usdkrw_rate` / `get_usdkrw_rate` / `require_usdkrw_rate` | 거래소 인자를 받도록 변경 |
| `app/services/live_store.py:86,163,167,230,243` | `AnyRate`, `get_usdkrw_rate()`, `require_usdkrw_rate_or_db()` | 거래소별 조회로 변경 |
| `app/services/spread_service.py:144` | `fwd/rev` 계산 | §3.1 공식 적용 |
| `app/services/premium_service.py` | `_build_entry`, `resolve_usdkrw_rate` | 방향에 맞는 환율 선택 |
| `app/services/arbitrage_service.py:128,394` | `_factor()`가 단일 rate로 환산 | 매수측/매도측 각각 맞는 환율 |
| `app/services/scan_service.py` · `matrix_service.py` · `comparison_service.py` | 단일 rate 사용 | 동일 전환 |
| `app/api/routes/rate.py` | `GET /rate` 단일 값 + `source: "hana"` | 거래소별 ask/bid 반환 |
| `app/models/spread.py:124` `SpreadsResult.rate` | 응답 최상위 단일 float | **FE 계약 변경** — §7 참조 |
| `app/models/premium.py` · `arbitrage.py` · `scan.py` · `comparison.py` · `refresh.py` | `usd_krw_rate: float` | 필드 재설계 |
| `app/history/hana.py` | 하나은행 스크래퍼 (198줄) | **파일 삭제** |
| `app/history/service.py:55,74,288,312,334` | `premium_from_closes`, `record_usdkrw_observation`, `collect_usdkrw_events` | 하나은행 의존 제거, 환율 소스를 KRW-USDT 캔들로 |
| `app/core/config.py:77` `hana_base_url` | 하나은행 URL | 삭제 |
| `scripts/bulk_archive.py` · `scripts/backfill_history.py` | 하나은행 환율로 백필 | KRW-USDT 캔들 종가로 교체 |
| `app/db/models.py:133` `PremiumArchive` | 하나은행 기준 데이터 적재됨 | **테이블 비우고 재백필** |

---

## 5. 작업 순서

각 단계마다 **검증**까지 끝내고 다음으로 간다. 단계별 커밋 권장.

### 1단계 — 환율 수집 경로 교체
- `_domestic_market` 결과에서 dom별 `USDT/KRW` 호가의 ask1/bid1을 뽑아내는 경로 추가
- `_usdkrw_rate`(하나은행) 제거
- **검증:** `POST /refresh` 후 DB에 업비트·빗썸 각각의 ask/bid가 들어있고, 값이 `curl https://api.upbit.com/v1/orderbook?markets=KRW-USDT` 실측과 일치

### 2단계 — 저장 구조 변경
- `UsdKrwRate` 모델을 거래소별 + ask/bid로 재설계, repository·live_store 함수 시그니처 변경
- **검증:** `pytest tests/test_repository.py tests/test_live_store.py` 통과

### 3단계 — 계산 서비스 6개 전환
- `spread_service` → `premium_service` → `arbitrage_service` → `scan/matrix/comparison` 순
- **검증:** 각 서비스 테스트 통과 + 아래 **회귀 성질** 확인
  > `usdt_ask == usdt_bid == R` 로 고정하면 결과가 기존 단일환율 `R` 일 때와 **완전히 같아야 한다.** 이게 깨지면 공식 적용이 틀린 것이다.

### 4단계 — 응답 모델 · 라우트
- `GET /rate` 재설계, 각 응답 모델의 `usd_krw_rate` 필드 정리
- **검증:** `pytest tests/test_api.py` 통과, `/docs` 스키마 확인

### 5단계 — 이력 정리
- `hana.py` 삭제, `history/service.py`에서 하나은행 의존 제거
- `premium_archive` 테이블 비우기 (**파괴적 — 실행 전 진중에게 확인받을 것**)
- 백필 스크립트를 KRW-USDT 캔들 종가 기준으로 교체 후 재실행
- **검증:** `/history/premium` 응답이 새 기준으로 나오고, 라이브 `/spreads` 값과 같은 시각대에서 크게 어긋나지 않음

### 6단계 — 전체 검증
- `pytest` 전량 통과 (현재 기준 303개 + 신규)
- 배포 후 수동 대조 (§6)

---

## 6. 완료 판정 기준

1. `pytest` 전량 통과
2. **회귀 성질**: ask==bid로 고정 시 기존 결과와 동일 (3단계 검증 항목)
3. **방향 분리 확인**: `usdt_ask=1400, usdt_bid=1390` 고정 시 fwd와 rev가 서로 다른 환율을 쓴 값이 나온다 (같은 값이면 실패)
4. **dom 분리 확인**: 같은 코인의 `dom=upbit` 행과 `dom=bithumb` 행이 서로 다른 환율을 쓴다
5. **실측 대조**: 배포 후 임의 코인 1개를 골라 손계산과 응답값 일치
   ```bash
   # 예시 — BTC
   curl -s 'https://api.upbit.com/v1/orderbook?markets=KRW-BTC,KRW-USDT'
   curl -s 'https://api.binance.com/api/v3/ticker/bookTicker?symbol=BTCUSDT'
   # fwd = btc_krw_bid1 / (btc_usdt_ask1 × usdt_krw_ask1) - 1
   ```
6. 응답 어디에도 하나은행 값이 남아있지 않음 (`grep -ri hana app/` 결과 없음)

---

## 7. FE 영향 (별도 처리 필요)

`app/models/spread.py:124` `SpreadsResult.rate`가 **응답 최상위 단일 float**다. D3(dom별 환율)이 적용되면 이 필드로는 표현이 안 된다.

권장: `rate`를 각 `SpreadRow` 안으로 내리거나(`rateAsk`/`rateBid`), 최상위에 거래소별 맵으로 둔다. **어느 쪽이든 FE 계약 변경이므로 `marketlens-fe`(`/Users/jin/Documents/marketlens-fe`) 쪽 작업이 동반된다.** 이 지시서 범위 밖 — 진중·원규가 계약 형태를 먼저 합의할 것.

---

## 8. 스코프 가드 — 하지 말 것

- **수수료 도입 금지.** 현재 어느 계산에도 거래·출금 수수료가 없다. 이건 별건이며 이번 작업에 섞지 말 것
- **슬리피지 확장 금지.** `/spreads`는 최우선 호가 1단만 본다는 현재 설계를 유지한다. 환율에도 ask1/bid1 **1단만** 쓴다 (USDT 호가창을 walk 하지 말 것)
- **무관한 리팩터링·주석 정리 금지.** 환율 경로에 직접 닿는 코드만 수정
- **`market_snapshots` 교집합 필터 자체를 건드리지 말 것.** USDT를 김프 계산 대상 코인으로 통과시키면 안 된다 — 환율 전용 경로로만 빼낼 것

---

## 9. 열린 질문 (구현 전 진중에게 확인)

1. **USDT 호가 결측 시 폴백.** 그 dom 행 전체를 `status: fail`로 내릴지, 아니면 직전 저장값을 쓸지. (하나은행이 사라지므로 기존 폴백이 없어진다)
2. **빗썸 KRW-USDT 유동성.** 업비트 대비 얇아 ask1이 튈 수 있다. 이상치 가드를 둘지, 아니면 있는 그대로 노출할지
3. **과거 이력의 한계 명시.** 캔들에는 호가가 없어 **과거 ask1/bid1은 복원 불가**다. 백필은 USDT 종가로 근사하며 fwd/rev가 대칭으로 나온다 (`history/service.py:55` 주석과 동일한 한계). 이 차이를 FE 기록/통계 탭에 표기할지
4. **`premium_archive` 폐기 실행 시점.** 재백필에 시간이 걸리므로 배포 타이밍 조율 필요

---

## 부록 — 참고 커밋

```
9ef7963  refactor!: 환율을 하나은행 고시 USD/KRW 로 통일   ← 이번에 되돌리는 대상
c1838b8  refactor: 환율을 가리키던 fx 이름을 usdkrw 로 바꾼다
9d90663  perf: 대량 백필의 환율 수집을 실행당 1회로 줄인다   ← 하나은행 백필 최적화, 함께 정리 대상
```

`9ef7963^`(직전 커밋)에 거래소별 환율 구조(`krw_rates` 테이블, `upsert_krw_rate`, `_krw_rate`)가 남아 있다. **구조 참고용으로만** 보고, 값 선택 로직(last 체결가)은 따라가지 말 것.
