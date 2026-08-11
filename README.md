# marketlens-be

거래소 간 가격차(김프·역프)를 계산하는 FastAPI 백엔드.

데이터 흐름은 **수집과 조회 2단 구조**다.

- **수집** — `POST /refresh` 가 거래소 공개 API 를 비동기로 동시 호출해
  시세·호가·입출금 상태·환율을 **PostgreSQL 에 저장**한다.
  거래소를 실제로 호출하는 경로는 이것 하나뿐이다.
- **조회** — 그 외 모든 API 는 거래소를 직접 부르지 않고 **DB 만 읽어** 계산한다.

현재 지원: **업비트**, **빗썸**, **바이낸스**(현물 USDT 마켓)

## 아키텍처

```
             ┌─────────────── 수집 (거래소를 부르는 유일한 경로) ───────────────┐
             │                                                                │
POST /refresh ──▶ 업비트 · 빗썸 · 바이낸스 공개 API (+ 입출금 상태 프라이빗 API)
             │                                                                │
             ▼                                                                │
      PostgreSQL ── market_snapshots   거래소 × 코인: 현재가 · 호가 · 입출금 상태
                 └─ krw_rates          국내 거래소별 KRW-USDT 환율 (마지막 체결가)
             ▲
             │
             └─────────────── 조회 (거래소 호출 없음, DB 만 읽음) ──────────────
                  GET /rate /orderbook /compare /premium* /slippage /matrix /arbitrage
```

- 가격·호가는 **환산 없이 그 거래소 통화 그대로** 저장한다 (업비트·빗썸 = KRW,
  바이낸스 = USDT). 원화 환산은 조회 시점에 `krw_rates` 를 곱해서 한다.
- 조회 API 는 데이터가 오래돼도 그대로 계산한다. 신선도는 응답의
  `data_oldest_at` / `updated_at` 계열 필드로 확인하고, 오래됐으면
  `POST /refresh` 로 갱신한다.
- 테이블은 앱 기동 시 자동 생성된다 (별도 마이그레이션 불필요).

## 실행 (로컬)

```bash
# 1. 로컬 PostgreSQL + Adminer 기동
#    (PostgreSQL: localhost:5432, 사용자/비밀번호/DB 모두 marketlens
#     Adminer 웹 뷰어: http://localhost:8080)
docker compose -f docker-compose.dev.yml up -d

# 2. 환경변수 준비 — 예시 파일을 복사한 뒤 필요한 값을 채운다
#    UPBIT_API_KEY / UPBIT_SECRET_KEY, BINANCE_API_KEY / BINANCE_SECRET_KEY 는
#    입출금 가능 여부 조회용 **선택사항** — 비워두면 해당 값만 null 로 저장된다
cp .env.example .env

# 3. 서버 기동 (기동 시 테이블 자동 생성)
pip install -r requirements.txt
uvicorn app.main:app --reload

# 4. 첫 수집 — DB 를 채운다
curl -X POST "http://localhost:8000/refresh"

# 5. 이제 조회 API 사용 가능
curl "http://localhost:8000/premium?sym=BTC"
```

Swagger UI: http://localhost:8000/docs

### DB 접속

```bash
# psql 직접 접속
psql postgresql://marketlens:marketlens@localhost:5432/marketlens

# 또는 컨테이너 안에서
docker exec -it marketlens-db psql -U marketlens
```

Adminer (http://localhost:8080): 시스템 `PostgreSQL`, 서버 `db`,
사용자·비밀번호·데이터베이스 모두 `marketlens`.

## 엔드포인트

| | 무엇을 하나 |
|---|---|
| `POST /refresh` | **DB 갱신** — 거래소에서 시세·호가·입출금 상태·환율을 수집해 저장 (거래소를 부르는 유일한 경로) |
| `GET /orderbook/{id}` | 거래소 한 곳의 호가창 (DB 스냅샷) |
| `GET /compare` | 여러 거래소 가격을 한 통화로 환산해 비교 + 차익 스프레드 |
| `GET /rate` | **USDT/KRW 환율** — `krw_rates` 저장값 (국내 거래소별) |
| `GET /premium` | **코인 검색** — 김프와 역김프를 한 번에 |
| `GET /premium/fwd` | **김프** — 해외에서 사와 국내에 팔 때 수익률 |
| `GET /premium/rev` | **역김프** — 국내에서 사서 해외에 팔 때 수익률 |
| `GET /premium/scan` | **전종목 스캔** — 김프 1등 · 역김프 1등 찾기 |
| `GET /spreads` | **스프레드 테이블** — 전 페어 김프/역프 (FE 계약 1:1) |
| `GET /slippage/{id}` | **슬리피지** — 시장가로 거래하면 평균 체결가가 얼마나 나빠지나 |
| `GET /matrix` | **전 코인 매트릭스** — 코인별 최대 김프·최대 역프 조합 + 슬리피지 (단일 `amount_krw`) |
| `GET /arbitrage` | **금액을 넣으면 실제로 얼마 남나** (호가 소진 · 슬리피지 반영) |
| `GET /exchanges` | 지원 거래소 목록 |
| `GET /health` | 헬스체크 |

```bash
# 수집 — 조회 전에 먼저 한 번
curl -X POST "http://localhost:8000/refresh"

# 업비트 BTC 호가 5단계
curl "http://localhost:8000/orderbook/upbit?symbol=BTC/KRW&depth=5"

# 거래소 간 가격 비교 (원화 환산)
curl "http://localhost:8000/compare?sym=BTC"

# 환율 (거래소별로 값이 다르다)
curl "http://localhost:8000/rate?exchange=bithumb"

# 코인 검색 — 김프 + 역김프 동시
curl "http://localhost:8000/premium?sym=BTC"

# 국내 거래소 선택 (업비트 / 빗썸)
curl "http://localhost:8000/premium?sym=BTC&dom=bithumb"

# 김프 — 해외 매수 → 국내 매도
curl "http://localhost:8000/premium/fwd?sym=BTC"

# 역김프 — 국내 매수 → 해외 매도
curl "http://localhost:8000/premium/rev?sym=BTC"

# 전종목에서 김프/역김프 1등 찾기 (유동성 1,000만원 이상만)
curl "http://localhost:8000/premium/scan?min_liquidity_krw=10000000"

# 1억원어치 사면 슬리피지 몇 %?
curl "http://localhost:8000/slippage/upbit?symbol=BTC/KRW&side=buy&amount=100000000"

# 전 코인 매트릭스 — 1,000만원 기준 최대 김프·역프 조합
curl "http://localhost:8000/matrix?amount_krw=10000000"

# 1,000만원 넣으면 실제로 얼마 남나
curl "http://localhost:8000/arbitrage?sym=BTC&amount=10000000"
```

## 문서

- **[docs/API.md](docs/API.md)** — 전체 API 명세, DB 스키마, 수집 대상,
  에러 코드, 거래소 추가 방법
- **[docs/ROADMAP.md](docs/ROADMAP.md)** — 목업 완성까지 남은 작업을 난이도 순으로 정리한 단계별 계획

## 구조

```
app/
├── main.py                 # FastAPI 앱 · lifespan(HTTP 풀 + DB 엔진) · 예외 핸들러
├── core/
│   ├── config.py           # 설정 (DATABASE_URL, 수집 튜닝, API 키)
│   ├── http.py             # 공용 AsyncClient (커넥션 재사용) · 호출 수 계측
│   └── errors.py           # 도메인 예외 (HTTP 상태 코드 포함)
├── db/
│   ├── models.py           # 테이블 2개 — market_snapshots, krw_rates
│   ├── database.py         # 비동기 엔진·세션, 기동 시 테이블 생성
│   └── repository.py       # DB 읽기·쓰기의 단일 창구 (조회 API 는 전부 여기를 거침)
├── models/                 # 응답 도메인 모델 (pydantic)
│   ├── symbol.py           # BASE/QUOTE 파싱
│   ├── orderbook.py        # OrderBook, OrderBookLevel
│   ├── ticker.py           # Ticker, PriceSide
│   ├── bulk.py             # BulkQuote — 전종목 일괄 조회 스냅샷
│   ├── refresh.py          # RefreshResult — POST /refresh 응답
│   ├── scan.py             # ScanEntry, ScanResult
│   ├── spread.py           # FeedStatus, SpreadRow, SpreadsResult (FE 계약)
│   ├── comparison.py       # ExchangeQuote, ArbitrageSpread, ComparisonResult
│   ├── premium.py          # PremiumDirection, PremiumEntry, PremiumResult
│   ├── arbitrage.py        # ExecutionSide, ArbitrageResult
│   ├── slippage.py         # OrderSide, FillLevel, SlippageResult
│   └── matrix.py           # MatrixDirection, MatrixCoinEntry, MatrixResult
├── exchanges/
│   ├── base.py             # BaseExchange 추상 클래스 (is_domestic, 일괄 조회 포함)
│   ├── registry.py         # connectors/ 자동 스캔 → ID: 인스턴스 매핑
│   ├── connectors/         # ★ 거래소별 구현 — 파일 추가 시 자동 등록
│   │   ├── upbit.py
│   │   ├── bithumb.py
│   │   └── binance.py
│   └── private/
│       └── wallet_status.py # 입출금 가능 여부 조회 (수집기 전용, API 키 사용)
├── services/
│   ├── collector_service.py    # ★ 수집기 — 거래소를 호출해 DB 를 갱신하는 유일한 곳
│   ├── comparison_service.py   # 통화 환산 · 스프레드 계산
│   ├── premium_service.py      # 김프 / 역김프 계산 (방향별 호가 선택)
│   ├── scan_service.py         # 전종목 스캔 (티커 충돌 · 저유동성 탐지)
│   ├── spread_service.py       # 스프레드 테이블 (전 페어 fwd/rev + 유동성/신선도)
│   ├── orderbook_walk.py       # 호가창 소진 계산 (금액/수량 기준)
│   ├── slippage_service.py     # 단일 거래소 슬리피지
│   ├── matrix_service.py       # 전 코인 최대 김프·역프 매트릭스
│   └── arbitrage_service.py    # 금액 기준 차익 시뮬레이션
└── api/routes/             # HTTP 라우터 (refresh 포함)
```

**새 거래소 추가는 `connectors/` 에 파일 하나만 만들면 끝입니다.** 레지스트리가
`pkgutil` 로 폴더를 스캔해 자동 등록하므로 다른 파일은 수정하지 않습니다.
([가이드](docs/API.md#9-새-거래소-추가하기-자동-등록))

## 개발

```bash
pip install -r requirements-dev.txt
pytest tests -q          # 네트워크 없이 전 구간 검증 (DB 는 SQLite 인메모리로 대체)

# 로컬 DB (PostgreSQL 17 + Adminer 웹 뷰어 http://localhost:8080)
# ※ docker-compose.yml 은 EC2 배포용(be + db 컨테이너)이므로 로컬 DB는 dev 파일을 쓴다
docker compose -f docker-compose.dev.yml up -d

# __pycache__ 가 하위 폴더마다 생기는 게 싫다면 한 곳으로 모을 수 있다
export PYTHONPYCACHEPREFIX=~/.cache/pycache
```

## 배포 (EC2 + RDS)

`main` 에 머지되면 GitHub Actions 가 EC2 에 SSH 로 들어가
`git pull` + `docker compose up -d --build` 를 실행한다. `docker-compose.yml` 은
백엔드(be) 컨테이너만 띄우고, **DB 는 AWS RDS(PostgreSQL)** 를 쓴다 —
자동 백업·특정 시점 복원이 필요해서다.

**최초 1회 ① — RDS 인스턴스 만들기** (AWS 콘솔 → RDS → 데이터베이스 생성)

| 항목 | 값 |
|---|---|
| 엔진 | PostgreSQL 17 |
| 템플릿/크기 | 프리 티어 또는 db.t4g.micro, 스토리지 20GB gp3 |
| DB 인스턴스 식별자 | `marketlens` |
| 마스터 사용자 / 암호 | `marketlens` / 강한 비밀번호 |
| 초기 데이터베이스 이름 | `marketlens` (추가 구성에서 지정) |
| VPC | **EC2 와 같은 VPC**, 퍼블릭 액세스 **아니요** |
| 백업 | 자동 백업 활성화, 보존 기간 7일~ |

생성 후 **보안 그룹**: RDS 의 보안 그룹 인바운드에 `PostgreSQL(5432)` 규칙을
추가하되, 소스를 **EC2 의 보안 그룹**으로 지정한다 (IP 전체 개방 금지).

**최초 1회 ② — EC2 에 `.env` 만들기.** `.env` 는 git 에 올라가지 않으므로
서버에 직접 만든다. 없으면 배포가 실패하도록 워크플로가 막는다.

```bash
# EC2 의 ~/marketlens-be/.env
DATABASE_URL=postgresql://marketlens:<비밀번호>@<RDS엔드포인트>:5432/marketlens
REFRESH_TOKEN=랜덤문자열                # POST /refresh 보호 (openssl rand -hex 32)
UPBIT_API_KEY=...                       # 입출금 상태 조회용 (선택)
UPBIT_SECRET_KEY=...
BINANCE_API_KEY=...
BINANCE_SECRET_KEY=...
SCAN_EXCLUDED_BASES=["AI","PROS"]       # 티커 충돌 코인 제외
```

- RDS 엔드포인트는 콘솔의 인스턴스 상세 → "엔드포인트" (예:
  `marketlens.xxxx.ap-northeast-2.rds.amazonaws.com`).
- 테이블은 be 첫 기동 때 자동 생성되고, 데이터는 `POST /refresh` 로 채운다.
- RDS 는 퍼블릭 액세스가 없으므로 로컬에서 직접 붙을 수 없다. 확인은 EC2 에서:
  `docker compose exec be python -c "..."` 또는 EC2 에 psql 설치 후 접속.
- 업비트 입출금 상태를 쓰려면 업비트 Open API 에 **EC2 의 IP** 도 등록해야 한다.
- 시세 갱신은 자동이 아니다. EC2 crontab 등으로 주기 호출한다:
  `* * * * * curl -s -X POST -H "X-Refresh-Token: <토큰>" http://localhost:8000/refresh > /dev/null`

## 협업 규칙

FE(marketlens-fe)·BE(marketlens-be) 공통 규칙. 이 문서가 단일 기준이다.

### Git

**브랜치** — `main`은 보호 브랜치: **직접 push 금지**, 모든 변경은 PR로만 병합한다.
작업은 아래 접두사를 붙인 브랜치에서 한다.

| 접두사 | 용도 | 예시 |
|---|---|---|
| `feat/` | 새 기능 | `feat/okx-connector` |
| `fix/` | 버그 수정 | `fix/fx-timeout` |
| `chore/` | 빌드·설정·잡무 (기능 변화 없음) | `chore/pin-deps` |

```bash
git switch main && git pull
git switch -c feat/작업-이름
```

**커밋** — Conventional Commits: `<타입>: <설명>` 형식, 설명은 명령형·50자 이내.

| 타입 | 언제 |
|---|---|
| `feat` | 새 기능 |
| `fix` | 버그 수정 |
| `docs` | 문서만 변경 |
| `refactor` | 동작 그대로, 구조만 개선 |
| `test` | 테스트 추가·수정 |
| `chore` | 빌드 설정, 의존성 등 나머지 |

예: `feat: OKX 커넥터 추가`

**PR**

- 상대방 **승인 1개** + **CI 통과** 필수 (통과 전 머지 불가)
- **`main` 에 머지되면 EC2 에 자동 배포된다** (`.github/workflows/deploy.yml`)
- 가능하면 **300줄 이하**로 쪼개서 올린다
- 본문은 3줄 템플릿 (`.github/PULL_REQUEST_TEMPLATE.md` 자동 적용):

```markdown
**무엇을:** OKX 커넥터 추가 (호가·티커·일괄 조회)
**왜:** 해외 거래소 커버리지 확대 (Closes #12)
**테스트:** pytest 통과, 로컬에서 /orderbook/okx 수동 확인
```

**이슈** — 기능 단위로 이슈를 먼저 만들고, PR 본문에 `Closes #이슈번호`를 적어
연결한다. 머지되면 이슈가 자동으로 닫힌다.

### 코드

- **BE**: ruff (lint + format) — 커밋 전 `ruff check . && ruff format .` 실행
  (CI는 pytest 만 검사). 타입힌트 필수. 신규 로직에는 pytest
  테스트를 함께 작성한다 (기존 코드에 소급 적용은 안 함).
- **FE**: ESLint + Prettier 도입 예정 (현재 미설정 → 진중이 CI와 함께 세팅),
  tsc strict 유지.
- **시크릿**: `.env`에만 둔다. **커밋 금지** — 대신 키 이름만 담은
  `.env.example`을 제공한다.

### API

- **스키마 변경 시** BE의 API 문서(`docs/API.md`)와 FE의 `types.ts`를 같은
  PR(저장소가 다르므로 서로 링크한 동시 PR)에서 함께 수정한다. 코드와 계약
  문서가 어긋난 상태를 만들지 않는다.
- **버전**: `/v1` prefix 여부 → 결정:
- **JSON 필드 네이밍**: BE pydantic 모델은 **snake_case**, FE `types.ts`는
  **camelCase**.
