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
REFRESH_TOKEN=랜덤문자열                # POST /refresh 보호 (openssl rand -hex 32)
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

## 주기 작업 (앱 내부 루프)

시세·호가·환율 갱신은 **앱이 스스로 돈다.** be 컨테이너가 뜨면 수집 루프가
`COLLECT_INTERVAL_SECONDS`(기본 1초) 간격으로 사이클을 반복한다. 별도 스케줄러도
crontab 도 필요 없다.

김프/역프 기록(`premium_archive`)과 입출금 상태 조회는 라이브보다 느리게 돈다 —
각각 `ARCHIVE_INTERVAL_SECONDS`, `WALLET_REFRESH_SECONDS`(기본 60초) 주기다.

### ⚠️ 기존 crontab 은 반드시 제거할 것

예전에는 EC2 crontab 이 매분 `POST /refresh` 를 불렀다. 그대로 두면 앱 내부
루프와 **이중으로 수집**한다. 배포 후 EC2 에서 지운다:

```bash
crontab -l                      # 먼저 확인
crontab -e                      # /refresh 를 부르는 줄을 지운다
crontab -l | grep refresh       # 아무것도 안 나와야 한다
```

crontab 방식은 `curl ... > /dev/null` 로 실패를 전부 버려서, 실제로 8시간
결측이 났는데도 아무도 알지 못했다. 앱 내부 루프는 실패를 로그로 남기고
연속 실패 시 백오프(최대 60초)를 건다.

### ⚠️ uvicorn 워커는 1개여야 한다

수집 루프가 프로세스 안에 있으므로 `--workers 2` 이상으로 띄우면 워커마다
루프가 돌아 중복 수집이 된다. 현재 `Dockerfile` 은 워커 옵션이 없어 단일
프로세스다 — 늘릴 거라면 수집을 별도 프로세스로 떼야 한다.

## 김프 기록 대량 채우기 (배포 후 최초 1회)

과거 3개월치 김프/역프 기록을 채운다. 업비트 초봉이 3개월 롤링이라 **배포 후
바로 돌리는 게 이득**이다 (미룰수록 과거를 잃는다). 중단돼도 재실행하면
이어서 진행된다.

```bash
docker compose exec be python -m scripts.bulk_archive --bases BTC
```

옵션·소요 시간은 [HISTORY.md](HISTORY.md#대량-채우기-실행법-최초-1회--필요할-때) 참고.

## 구조 마이그레이션

별도 작업이 필요 없다 — **앱이 기동할 때 이전 구조의 잔재를 자동 정리**한다
(구 압축 이력 테이블 price_points/price_chunks/fx_points/fx_chunks/
history_cursors 와 krw_rates 를 DROP — `app/db/views.py` CLEANUP_DDL).

## 트러블슈팅

- 배포 직후 `/rate` 가 404 → 아직 환율 수집 전이다. refresh 가 한 번
  돌면 채워진다.
- `/history/premium` 이 404 (구간에 기록 없음) → refresh cron 이 없거나,
  과거 구간은 bulk_archive 를 아직 안 돌린 상태다.
- refresh 가 오래 멈췄다 재개되면 그 사이 기록이 비는데, bulk_archive 를
  돌리면 마지막 기록 이후 구간이 초 단위로 채워진다.
