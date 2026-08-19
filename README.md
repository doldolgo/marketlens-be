# marketlens-be

거래소 간 가격차(김프·역프)를 계산하는 FastAPI 백엔드.

현재 지원: **업비트**, **빗썸**, **바이낸스**(현물 USDT 마켓)

## 아키텍처

데이터 흐름은 **수집과 조회 2단 구조**다. 거래소를 실제로 호출하는 경로는
수집 엔드포인트 두 개뿐이고, 나머지 모든 API 는 DB 만 읽어 계산한다.

```
 ┌────────────── 수집 (외부를 부르는 유일한 경로) ────────────────────┐
 │                                                                   │
 │  POST /refresh ──▶ 업비트·빗썸·바이낸스 시세/호가 + 하나은행 환율   │
 │       │                                                           │
 │       ├─▶ market_snapshots  거래소×코인: 현재가·호가·입출금 (UPSERT)│
 │       ├─▶ usdkrw_rate           통일 환율 (하나은행 USD/KRW) 1행       │
 │       ├─▶ premium_archive   갱신 직후 김프/역프 계산해 기록 추가    │
 │       └─▶ platform_status   플랫폼당 1행 — 수신 시각·실패율 카운터  │
 │                                                                   │
 │  scripts/bulk_archive.py ──▶ 아카이브 밖 과거 구간을 거래소         │
 │                              캔들(초 단위)로 계산해 대량 채움       │
 └───────────────────────────────────────────────────────────────────┘
        ▲
        └────────── 조회 (외부 호출 없음, DB 만 읽음) ─────────────────
   실시간 스프레드 창: GET /spreads /rate /orderbook /compare /premium*
                       /slippage /matrix /arbitrage
   기록/통계 창:       GET /history/premium /history/status
```

원칙 세 가지:

- **환산 없이 저장** — 가격·호가는 그 거래소 통화 그대로 (업비트·빗썸 = KRW,
  바이낸스 = USDT). 원화 환산은 조회 시점에 통일 환율(`usdkrw_rate`,
  하나은행 고시 USD/KRW)을 곱해서 한다.
- **신선도는 응답이 알려준다** — 조회 API 는 데이터가 오래돼도 그대로 계산하고,
  `data_oldest_at` / `updated_at` 계열 필드로 언제 데이터인지 표시한다.
- **스냅샷은 갱신, 기록은 누적** — market_snapshots 는 코인을 찾아 UPSERT 만
  하고(삭제 없음), 김프/역프 기록(premium_archive)은 계속 쌓인다
  (압축 없이 일반 행 — 용량이 문제가 되면 그때 재검토).

테이블은 앱 기동 시 자동 생성된다 (별도 마이그레이션 불필요).

## 빠른 시작 (로컬)

```bash
# 1. 로컬 PostgreSQL + Adminer(웹 DB 뷰어 :8080) 기동
docker compose -f docker-compose.dev.yml up -d

# 2. 환경변수 — 예시를 복사 (API 키는 입출금 상태 조회용 선택사항)
cp .env.example .env

# 3. 서버 기동 (테이블·뷰 자동 생성)
pip install -r requirements.txt
uvicorn app.main:app --reload

# 4. 첫 수집 → 조회
curl -X POST "http://localhost:8000/refresh"
curl "http://localhost:8000/premium?sym=BTC"
```

Swagger UI: http://localhost:8000/docs — 전 엔드포인트를 화면에서 실행해볼 수 있다.

## 문서

| 문서 | 내용 |
|---|---|
| **[docs/API.md](docs/API.md)** | 전체 API 명세 — 엔드포인트, 파라미터, 응답, 에러, 거래소 추가법 |
| **[docs/DB.md](docs/DB.md)** | DB 구조 — 테이블·컬럼·단위, 읽기 뷰 |
| **[docs/HISTORY.md](docs/HISTORY.md)** | 김프/역프 기록 시스템 — 조회 API, 대량 채우기, 운영 |
| **[docs/DEPLOY.md](docs/DEPLOY.md)** | 배포 — EC2+RDS 초기 설정, 수집 루프, 마이그레이션 |
| **[docs/ROADMAP.md](docs/ROADMAP.md)** | 단계별 구현 계획 |

## 구조

```
app/
├── main.py                 # FastAPI 앱 · lifespan(HTTP 풀 + DB 엔진) · 예외 핸들러
├── core/                   # 설정 · 공용 HTTP 클라이언트 · 도메인 예외
├── db/
│   ├── models.py           # 테이블 정의 (스냅샷·환율·김프 기록·플랫폼 상태)
│   ├── views.py            # 사람이 읽는 DB 뷰 (연도 포함 KST 시각)
│   ├── database.py         # 비동기 엔진·세션, 기동 시 테이블·뷰 생성
│   └── repository.py       # 라이브 테이블 읽기·쓰기의 단일 창구
├── history/                # ★ 김프/역프 기록(아카이브) 서브시스템
│   ├── upbit.py            #   업비트 초봉 수집기 (대량 채우기용)
│   ├── binance.py          #   바이낸스 1초봉 수집기
│   ├── hana.py             #   하나은행 고시환율 수집기
│   └── service.py          #   김프 계산식 · 대량 채우기 로직
├── models/                 # 응답 도메인 모델 (pydantic)
├── exchanges/
│   ├── registry.py         # connectors/ 자동 스캔 → ID: 인스턴스 매핑
│   ├── connectors/         # ★ 거래소별 구현 — 파일 추가 시 자동 등록
│   └── private/            # 입출금 가능 여부 조회 (API 키 사용)
├── services/               # 계산 로직 (수집기 · 김프 · 스캔 · 슬리피지 · 차익 ...)
└── api/routes/             # HTTP 라우터 (모듈 docstring 에 테스트 URL 예시)
scripts/
└── bulk_archive.py         # 김프 기록 대량 채우기 (아카이브 밖 구간, 재개 가능)
```

**새 거래소 추가는 `connectors/` 에 파일 하나만 만들면 끝.** 레지스트리가
폴더를 스캔해 자동 등록한다. ([가이드](docs/API.md#9-새-거래소-추가하기-자동-등록))

## 개발

```bash
pip install -r requirements-dev.txt
pytest tests -q          # 네트워크 없이 전 구간 검증 (DB 는 SQLite 인메모리)
ruff check . && ruff format .   # 커밋 전
```

## 배포

`main` 머지 → GitHub Actions 가 EC2 에 자동 배포. DB 는 AWS RDS.
초기 설정·수집 루프·백필·마이그레이션은 **[docs/DEPLOY.md](docs/DEPLOY.md)** 참고.

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
