# 배포 (EC2 + RDS)

`main` 에 머지되면 GitHub Actions 가 EC2 에 SSH 로 들어가
`git pull` + `docker compose up -d --build` 를 실행한다. `docker-compose.yml` 은
백엔드(be) 컨테이너만 띄우고, **DB 는 AWS RDS(PostgreSQL)** 를 쓴다 —
자동 백업·특정 시점 복원이 필요해서다.

## 최초 1회 ① — RDS 인스턴스 만들기

AWS 콘솔 → RDS → 데이터베이스 생성:

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

## 최초 1회 ② — EC2 에 `.env` 만들기

`.env` 는 git 에 올라가지 않으므로 서버에 직접 만든다. 없으면 배포가
실패하도록 워크플로가 막는다.

```bash
# EC2 의 ~/marketlens-be/.env
DATABASE_URL=postgresql://marketlens:<비밀번호>@<RDS엔드포인트>:5432/marketlens
REFRESH_TOKEN=랜덤문자열                # POST /refresh·/history/sync 보호 (openssl rand -hex 32)
UPBIT_API_KEY=...                       # 입출금 상태 조회용 (선택)
UPBIT_SECRET_KEY=...
BINANCE_API_KEY=...
BINANCE_SECRET_KEY=...
SCAN_EXCLUDED_BASES=["AI","PROS"]       # 티커 충돌 코인 제외
HISTORY_BASES=["BTC"]                   # 변동 이력을 수집할 코인 (선택, 기본 BTC)
```

- RDS 엔드포인트는 콘솔의 인스턴스 상세 → "엔드포인트" (예:
  `marketlens.xxxx.ap-northeast-2.rds.amazonaws.com`).
- 테이블·읽기 뷰는 be 첫 기동 때 자동 생성된다.
- RDS 는 퍼블릭 액세스가 없으므로 로컬에서 직접 붙을 수 없다. 확인은 EC2 에서
  psql 로 하거나, DB GUI 의 **SSH 터널**(Host=EC2, DB=RDS 엔드포인트)로 접속한다.
- 업비트 입출금 상태를 쓰려면 업비트 Open API 에 **EC2 의 IP** 도 등록해야 한다.

## 주기 작업 (crontab)

시세·이력 갱신은 자동이 아니다 — EC2 crontab 으로 주기 호출한다:

```bash
# 라이브 시세·호가·환율 갱신 (매분)
* * * * * curl -s -X POST -H "X-Refresh-Token: <토큰>" http://localhost:8000/refresh > /dev/null
# 가격 변동 이력 증분 수집 (매분) — 김프/역프 통계용, /history/*
* * * * * curl -s -X POST -H "X-Refresh-Token: <토큰>" http://localhost:8000/history/sync > /dev/null
```

## 변동 이력 백필 (이력 기능 배포 후 최초 1회)

과거 3개월치를 채운다. 업비트 초봉이 3개월 롤링이라 **배포 후 바로 돌리는 게
이득**이다 (미룰수록 과거를 잃는다). 중단돼도 재실행하면 이어서 진행된다.

```bash
docker compose exec be python -m scripts.backfill_history --bases BTC
```

옵션·소요 시간은 [HISTORY.md](HISTORY.md#백필--과거-3개월-채우기-최초-1회) 참고.

## 환율 통일 마이그레이션 (1회성)

환율이 거래소별 KRW-USDT 에서 하나은행 고시 USD/KRW(`fx_rate`)로 통일되면서
예전 테이블은 더 이상 쓰이지 않는다. 배포 후 RDS 에서 정리한다:

```sql
DROP TABLE IF EXISTS krw_rates;
```

(테이블 자동 생성은 "없는 것만 만들기"라 옛 테이블을 지워주지 않는다.)

## 트러블슈팅

- 배포 직후 `/rate` 가 404 → 아직 환율 수집 전이다. refresh 나 sync 가 한 번
  돌면 채워진다.
- `/history/*` 가 404 (구간에 데이터 없음) → 백필을 안 돌렸거나 sync cron 이
  없는 상태다.
- sync 응답의 `failures` 에 특정 시리즈가 반복해서 나오면 해당 원천 API 장애
  — 커서 덕분에 복구되면 밀린 구간을 자동으로 따라잡는다.
