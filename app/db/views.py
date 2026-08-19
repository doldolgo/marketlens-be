"""사람이 읽는 DB 뷰 — GUI 뷰어(Beekeeper/Adminer/psql)용.

시각은 epoch 초(BIGINT)로 저장된다 — 연도까지 담긴 완전한 시각이지만 숫자
그대로는 읽기 어렵다. 그래서 **저장은 epoch 그대로 두고**, 연도가 포함된
KST 시각으로 보여주는 읽기 전용 뷰를 같이 만들어 둔다. 뷰는 저장 공간을
차지하지 않고 원본 테이블을 실시간으로 비춰줄 뿐이다.

    v_premium_archive   김프/역프 기록 — time_kst 로 "2026-08-14 09:00:00" 표기
    v_platform_status   플랫폼 상태 — 마지막 수신 시각(KST) + 입출금 실패율
    v_usdkrw_rate       라이브 환율 — 국내 거래소별 KRW-USDT ask/bid

PostgreSQL 전용이다 (테스트용 SQLite 에서는 만들지 않는다).
"""

from __future__ import annotations

#: 앱 기동 시 CREATE OR REPLACE 되는 뷰 정의 목록.
#: 원본 테이블 스키마가 바뀌면 여기도 같이 고친다.
VIEW_DDL: list[str] = [
    """
    CREATE OR REPLACE VIEW v_premium_archive AS
    SELECT dom,
           fx,
           base,
           to_timestamp(ts) AT TIME ZONE 'Asia/Seoul' AS time_kst,
           ts,
           round(fwd::numeric, 4) AS fwd_percent,
           round(rev::numeric, 4) AS rev_percent
    FROM premium_archive
    """,
    """
    CREATE OR REPLACE VIEW v_platform_status AS
    SELECT exchange,
           to_timestamp(last_received_ts) AT TIME ZONE 'Asia/Seoul'
               AS last_received_kst,
           spot_market_count,
           futures_market_count,
           dw_fail_count,
           update_count,
           CASE WHEN update_count > 0
                THEN round(dw_fail_count::numeric / update_count, 4)
                ELSE 0 END AS dw_fail_rate,
           updated_at
    FROM platform_status
    """,
    """
    CREATE OR REPLACE VIEW v_usdkrw_rate AS
    SELECT exchange,
           ask,
           bid,
           round((ask - bid)::numeric, 4) AS spread,
           updated_at
    FROM usdkrw_rate
    """,
]

#: 이전 구조(압축 이력)의 뷰·테이블 — 앱 기동 시 있으면 정리한다.
#: (테이블 자동 생성은 "없는 것만 만들기"라 옛것을 지워주지 않는다)
CLEANUP_DDL: list[str] = [
    # 환율 뷰는 컬럼 구성이 바뀌었다 — CREATE OR REPLACE 는 컬럼이 달라지면
    # 실패하므로 아래 VIEW_DDL 이 다시 만들기 전에 지운다.
    "DROP VIEW IF EXISTS v_usdkrw_rate",
    "DROP VIEW IF EXISTS v_price_points",
    "DROP VIEW IF EXISTS v_fx_points",
    "DROP VIEW IF EXISTS v_price_chunks",
    "DROP VIEW IF EXISTS v_fx_chunks",
    "DROP VIEW IF EXISTS v_history_cursors",
    "DROP TABLE IF EXISTS price_points",
    "DROP TABLE IF EXISTS price_chunks",
    "DROP TABLE IF EXISTS fx_points",
    "DROP TABLE IF EXISTS fx_chunks",
    "DROP TABLE IF EXISTS history_cursors",
    "DROP TABLE IF EXISTS krw_rates",
    # networks 컬럼은 나중에 생겼다. create_all 은 "없는 테이블만 만들기"라
    # **이미 있는 테이블에 컬럼을 붙이지 않는다** — 여기서 직접 붙인다.
    # (안 붙이면 조회는 메모리로 잘 되는데 저장만 조용히 실패한다)
    "ALTER TABLE market_snapshots "
    "ADD COLUMN IF NOT EXISTS networks JSONB DEFAULT '[]'::jsonb",
    # 입출금 가능 여부를 3-state 로 되돌린다 — "확인 불가"(null)를 "막힘"
    # (False)과 구분하기 위해서다. 예전엔 여기서 null 을 False 로 메우고
    # NOT NULL 을 걸었다 (커밋 4ae3847).
    #
    # **이 저장소에는 Alembic 이 없다.** create_all 은 "없는 것만 만들기"라
    # 이미 있는 컬럼의 nullable 을 바꾸지 않는다. 한 번 걸린 NOT NULL 은
    # 여기서 직접 풀지 않으면 남는다 — 그래서 마이그레이션이 DDL 로 상주한다.
    # (이미 풀린 컬럼에 다시 걸어도 에러가 나지 않는다. 매 기동 실행해도 안전)
    "ALTER TABLE market_snapshots "
    "ALTER COLUMN deposit_enabled DROP NOT NULL, "
    "ALTER COLUMN deposit_enabled DROP DEFAULT",
    "ALTER TABLE market_snapshots "
    "ALTER COLUMN withdrawal_enabled DROP NOT NULL, "
    "ALTER COLUMN withdrawal_enabled DROP DEFAULT",
    # 옛 이름 정리 — 환율 테이블 fx_rate → usdkrw_rate.
    # (`fx` 는 해외 거래소를 가리키는 이름이라 환율에는 쓰지 않는다)
    "DROP VIEW IF EXISTS v_fx_rate",
    "DROP TABLE IF EXISTS fx_rate",
]

#: create_all **보다 먼저** 실행되는 DDL — 테이블 모양 자체가 바뀐 경우.
#: create_all 은 "없는 테이블만 만들기"라 이미 있는 테이블의 컬럼을 갈아주지
#: 않는다. 옛 모양이 남아 있으면 여기서 지워야 새 모양으로 다시 만들어진다.
PRE_CREATE_DDL: list[str] = [
    # usdkrw_rate: 하나은행 단일 행(id/rate/source_time/round_no)
    #            → 거래소별 행(exchange/ask/bid).
    # 담긴 값이 은행 고시라 옮겨 담을 것이 없다 — 통째로 버리고 다시 만든다.
    # (다음 refresh 가 1초 안에 새 값을 채운다)
    """
    DO $$
    BEGIN
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'usdkrw_rate' AND column_name = 'rate'
        ) THEN
            DROP VIEW IF EXISTS v_usdkrw_rate;
            DROP TABLE usdkrw_rate;
        END IF;
    END $$
    """,
]
