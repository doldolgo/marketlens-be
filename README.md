# marketlens-be

거래소 공개 REST API 를 **직접** 호출해 현재 호가를 수집하고, 거래소 간 가격차를 계산하는 백엔드.

ccxt 라이브러리를 거치지 않고 원본 엔드포인트를 비동기로 동시 호출해
같은 작업 기준 **약 2.3배** 빠르다. ([벤치마크](docs/API.md#7-성능))

현재 지원: **업비트**, **바이낸스**(현물 + 선물)

## 실행

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Swagger UI: http://localhost:8000/docs

> 호가 조회는 전부 public API 라 **API 키가 필요 없다.**

## 엔드포인트

| | 무엇을 얻나 |
|---|---|
| `GET /orderbook/{id}` | 거래소 한 곳의 호가창 |
| `GET /compare` | 여러 거래소 가격을 한 통화로 환산해 비교 + 차익 스프레드 |
| `GET /premium` | 원화 가격이 해외보다 몇 % 비싼가 (김프) |
| `GET /arbitrage` | **금액을 넣으면 실제로 얼마 남나** (호가 소진 · 슬리피지 반영) |
| `GET /exchanges` | 지원 거래소 목록 |
| `GET /health` | 헬스체크 |

```bash
# 업비트 BTC 호가 5단계
curl "http://localhost:8000/orderbook/upbit?symbol=BTC/KRW&depth=5"

# 바이낸스 선물 호가
curl "http://localhost:8000/orderbook/binance?symbol=BTC/USDT&market_type=futures"

# 거래소 간 가격 비교 (원화 환산)
curl "http://localhost:8000/compare?base=BTC"

# 김치 프리미엄 (마지막 체결가 기준)
curl "http://localhost:8000/premium?base=BTC"

# 호가 중간가 기준
curl "http://localhost:8000/premium?base=BTC&price_basis=mid"

# 1,000만원 넣으면 실제로 얼마 남나
curl "http://localhost:8000/arbitrage?base=BTC&amount=10000000"
```

## 문서

- **[docs/API.md](docs/API.md)** — 전체 API 명세, 조회 가능 범위, 원본 거래소 API 주소, 호출 제한, 에러 코드, 거래소 추가 방법
- **[docs/ROADMAP.md](docs/ROADMAP.md)** — 목업 완성까지 남은 작업을 난이도 순으로 정리한 단계별 계획

## 구조

```
app/
├── main.py                 # FastAPI 앱 · 예외 핸들러
├── core/
│   ├── config.py           # 설정 (타임아웃, 커넥션 풀, 원화 기준 거래소)
│   ├── http.py             # 공용 AsyncClient (커넥션 재사용)
│   └── errors.py           # 도메인 예외 (HTTP 상태 코드 포함)
├── models/                 # 통일 도메인 모델
│   ├── symbol.py           # BASE/QUOTE 파싱
│   ├── orderbook.py        # OrderBook, OrderBookLevel
│   ├── ticker.py           # Ticker(마지막 체결가), PriceBasis
│   ├── comparison.py       # ExchangeQuote, ArbitrageSpread, ComparisonResult
│   ├── premium.py          # PremiumEntry, PremiumResult
│   └── arbitrage.py        # ExecutionSide, ArbitrageResult
├── exchanges/
│   ├── base.py             # BaseExchange 추상 클래스 (Template Method)
│   ├── registry.py         # connectors/ 자동 스캔 → ID: 인스턴스 매핑
│   └── connectors/         # ★ 거래소별 구현 — 파일 추가 시 자동 등록
│       ├── upbit.py
│       └── binance.py
├── services/
│   ├── fanout.py               # 다중 거래소 동시 호출 · 부분 실패 허용
│   ├── market_data_service.py  # 호가 / 마지막 체결가 조회
│   ├── comparison_service.py   # 통화 환산 · 스프레드 계산
│   ├── premium_service.py      # 김프 계산 (KRW / USDT / 환율)
│   ├── orderbook_walk.py       # 호가창 소진 계산 (시장가 체결 · 슬리피지)
│   ├── arbitrage_service.py    # 금액 기준 차익 시뮬레이션
│   └── fx.py                   # USDT/KRW 환율 (1초 TTL 캐시)
└── api/routes/             # HTTP 라우터
```

**새 거래소 추가는 `connectors/` 에 파일 하나만 만들면 끝입니다.** 레지스트리가
`pkgutil` 로 폴더를 스캔해 자동 등록하므로 다른 파일은 수정하지 않습니다.
([가이드](docs/API.md#9-새-거래소-추가하기-자동-등록))

## 개발

```bash
pip install pytest
pytest tests -q

# __pycache__ 가 하위 폴더마다 생기는 게 싫다면 한 곳으로 모을 수 있다
export PYTHONPYCACHEPREFIX=~/.cache/pycache

# ccxt 대비 속도 측정
pip install ccxt
python -m scripts.benchmark
```
