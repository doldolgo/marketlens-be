# MarketLens DB 구조

테이블 4개를 역할별로 설명한다. 모든 예시는 실제 저장 형태다.

```
실시간 (실시간 스프레드 창 담당) — POST /refresh 가 코인을 찾아 갱신
  market_snapshots   거래소×코인별 현재가·호가·입출금 상태
  usdkrw_rate        KRW-USDT 환율 (국내 거래소별 ask/bid) — 거래소당 1행

기록 (기록/통계 창 담당) — 계속 쌓이는 append 전용
  premium_archive    김프/역프 기록 (시각·코인·김프·역프)

플랫폼 상태
  platform_status    플랫폼당 1행 — 마지막 수신·마켓 수·입출금 실패율 재료
```

데이터 흐름 한눈에:

```
POST /refresh ─▶ market_snapshots 갱신(UPSERT) + usdkrw_rate 갱신
      │                └─▶ 실시간 스프레드 창 (GET /spreads 등)
      └─▶ 갱신 직후 김프/역프 계산 → premium_archive 에 한 줄 추가
                       └─▶ 기록/통계 창 (GET /history/premium)
      └─▶ platform_status 카운터 갱신 (업데이트 +1, 입출금 불가 관측 시 실패 +1)

scripts/bulk_archive.py ─▶ 아카이브 첫/마지막 시각 밖 구간을
                           거래소 캔들(초 단위)로 계산해 대량 채움
```

## 공통 규칙 — 시각과 단위

- **시각은 epoch 초**(BIGINT)로 저장한다. `1786634103` 같은 숫자지만 **연도까지
  담긴 완전한 시각**이다 (= 2026-08-14 00:15:03 KST). 사람이 읽을 때는
  아래 [읽기 뷰](#읽기-뷰)를 쓴다.
- 상대 시간("몇 초 뒤")은 저장하지 않는다 — 조회 API 가 계산해서 반환한다.
- 가격·호가의 단위는 `quote` 컬럼(또는 거래소)이 결정한다 — 업비트·빗썸 KRW,
  바이낸스 USDT. 환율은 USD 1달러당 원.

---

## `market_snapshots` — 거래소×코인별 현재 시세 (실시간 스프레드 창)

거래소 하나 × 코인 하나당 **딱 한 행**. `POST /refresh` 가 **코인을 찾아
UPSERT 만** 한다 — 지웠다 다시 만들지 않고, 이번 수집에 빠진 코인도 지우지
않는다. 상장폐지 코인은 행이 남되 `updated_at` 이 멈추므로 신선도 필드로
걸러진다.

| 컬럼 | 의미 |
|---|---|
| `exchange`, `base` (PK) | 거래소 ID(upbit/bithumb/binance) + 코인 심볼(BTC) |
| `native_symbol` | 거래소 원본 표기 (`KRW-BTC` / `BTCUSDT`) |
| `quote` | 가격 통화 (KRW 또는 USDT) |
| `price` | 마지막 체결가 (quote 통화 그대로) |
| `asks` / `bids` | 호가창 JSON `[[가격, 잔량], ...]` — 각 원소는 [호가 가격, 그 가격에 걸린 수량]. 누적 체결 가능액 10억원(`ORDERBOOK_MAX_AMOUNT_KRW`) 커버 깊이까지만 |
| `deposit_enabled` / `withdrawal_enabled` | 입금/출금 가능 여부 (API 키 없으면 null) |
| `price_timestamp` | 거래소가 준 시세 시각 (epoch **ms**) |
| `updated_at` | DB 저장 시각 — 신선도 판단 기준 |

예시 행: `(binance, BTC, BTCUSDT, USDT, 63747.2, asks=[[63745.61, 7.686], ...])`

## `usdkrw_rate` — KRW-USDT 환율, 국내 거래소당 1행

모든 원화 환산이 쓰는 환율 — 은행 고시가 아니라 **국내 거래소의 `KRW-USDT`
최우선 호가**다. 원화 ↔ 달러 전환이 실제로 일어나는 곳이 그 마켓이라, 거기
섞인 테더 프리미엄까지 포함한 값을 쓴다. refresh 가 매 회차 덮어쓴다.

| 컬럼 | 의미 |
|---|---|
| `exchange` (PK) | 국내 거래소 ID (`upbit` / `bithumb`) |
| `ask` | 최우선 매도호가 — 원화로 USDT 매수. **김프 계산에 쓴다** |
| `bid` | 최우선 매수호가 — USDT 를 원화로 매도. **역프 계산에 쓴다** |

예시 행: `(upbit, 1392.0, 1391.0)` — "업비트에서 USDT 를 1,392원에 사고 1,391원에 판다".

## `premium_archive` — 김프/역프 기록 (기록/통계 창)

기록 한 건당 한 행. **두 경로**로 쌓인다:

1. **실시간** — refresh 가 매 회차, 방금 갱신한 스냅샷에서 (국내 거래소 ×
   코인)마다 김프/역프를 계산해 추가. 체결측 호가 기준 (/spreads 와 동일식:
   김프 = 국내 bid ÷ (해외 ask × 환율) − 1, 역프 = 해외 bid × 환율 ÷ 국내 ask − 1).
2. **대량 채우기** — `scripts/bulk_archive.py` 가 아카이브의 **첫/마지막 시각
   밖 구간**을 업비트 초봉 × 바이낸스 1초봉 × 업비트 KRW-USDT 분봉으로 계산해 채움.
   캔들에는 호가가 없어 **종가 기준**이고, 셋 중 하나라도 변한 초마다 한 줄이다.

| 컬럼 | 의미 |
|---|---|
| `dom`, `fx`, `base`, `ts` (PK) | 국내 거래소 + 해외 거래소 + 코인 + 기록 시각(epoch 초) |
| `fwd` | 김프 % — 해외에서 사서 국내에 팔 때 수익률 |
| `rev` | 역프 % — 국내에서 사서 해외에 팔 때 수익률 |

예시 행: `(upbit, binance, BTC, 1786634103, 0.62, -0.71)`
→ "2026-08-14 00:15:03 KST 에 김프 +0.62%, 역프 −0.71%".

- 같은 (dom, fx, base, ts) 재삽입은 무시된다 — 실시간과 대량 채우기가 겹쳐도 안전.
- **압축하지 않는다** — 이전에 검토했던 압축 저장은 현 단계에서 제외했고,
  용량이 문제가 되면 그때 다시 고려한다. 참고 규모: 초 단위 김프 변동은
  코인당 하루 3~5만 행, 90일 ≈ 300~450만 행 (행당 ~60바이트 + 인덱스).

## `platform_status` — 플랫폼당 1행 (수신 상태·실패율)

refresh 가 market_snapshots 를 업데이트한 뒤 같은 플랫폼의 행을 함께 갱신한다.

| 컬럼 | 의미 |
|---|---|
| `exchange` (PK) | 플랫폼(거래소) ID |
| `last_received_ts` | 마지막 수신 시각 (epoch 초) — 수신 성공 시마다 갱신 |
| `spot_market_count` | 상장 현물 마켓 수 (이번 수집에서 관측) |
| `futures_market_count` | 상장 선물 마켓 수 (바이낸스 USDT 선물, 국내는 0) |
| `dw_fail_count` | **입금 또는 출금 불가 코인이 관측된** 업데이트 횟수 (회차당 최대 +1) |
| `update_count` | 전체 업데이트 횟수 (수신 성공마다 +1) |

**입출금 실패율 = `dw_fail_count` ÷ `update_count`** — `GET /history/status`
가 계산해서 준다. null(확인 불가)은 실패로 세지 않는다.

예시 행: `(upbit, 1786634103, 181, 0, 12, 1440)` → 실패율 0.83%.

---

## 읽기 뷰

epoch 초는 사람이 못 읽으므로, 앱 기동 시 읽기 전용 뷰를 만든다
(`app/db/views.py`, PostgreSQL 전용). GUI 뷰어에서는 이 뷰를 열면 된다.

| 뷰 | 보여주는 것 |
|---|---|
| `v_premium_archive` | 김프 기록 — `time_kst` (연도 포함 KST) + fwd/rev % |
| `v_platform_status` | 플랫폼 상태 — 마지막 수신(KST) + **실패율 계산 포함** |
| `v_usdkrw_rate` | 라이브 환율 + 고시 시각(KST) |

예시 (`SELECT * FROM v_premium_archive LIMIT 2`):

```
  dom  |   fx    | base |      time_kst       |     ts     | fwd_percent | rev_percent
-------+---------+------+---------------------+------------+-------------+------------
 upbit | binance | BTC  | 2026-08-14 09:00:03 | 1786665603 |      0.6234 |     -0.7101
```

앱 기동 시 **이전 구조의 잔재도 자동 정리**된다 — 구 압축 이력 테이블
(price_points/price_chunks/fx_points/fx_chunks/history_cursors)과 krw_rates 는
있으면 DROP 된다 (`views.py` 의 CLEANUP_DDL).

## 접속 방법

- **로컬**: `docker compose -f docker-compose.dev.yml up -d` 후
  `127.0.0.1:5432`, 계정/비밀번호/DB 모두 `marketlens`.
  웹 뷰어 Adminer: http://localhost:8080 (서버 `db`).
- **운영(RDS)**: 퍼블릭 액세스가 없어 EC2 경유 SSH 터널로 접속한다.
- 테이블·뷰는 앱 기동 시 자동 생성된다 (별도 마이그레이션 없음).
