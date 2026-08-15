"""사람이 읽는 DB 뷰 — GUI 뷰어(Beekeeper/Adminer/psql)용.

시각은 epoch 초(BIGINT)로 저장된다 — 연도까지 담긴 완전한 시각이지만 숫자
그대로는 읽기 어렵다. 그래서 **저장은 epoch 그대로 두고**, 연도가 포함된
KST 시각으로 보여주는 읽기 전용 뷰를 같이 만들어 둔다. 뷰는 저장 공간을
차지하지 않고 원본 테이블을 실시간으로 비춰줄 뿐이다.

    v_premium_archive   김프/역프 기록 — time_kst 로 "2026-08-14 09:00:00" 표기
    v_platform_status   플랫폼 상태 — 마지막 수신 시각(KST) + 입출금 실패율
    v_usdkrw_rate       라이브 환율 (고시 시각 포함)

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
    SELECT rate AS usd_krw,
           round_no,
           to_timestamp(source_time) AT TIME ZONE 'Asia/Seoul' AS published_kst,
           updated_at
    FROM usdkrw_rate
    """,
]

#: 이전 구조(압축 이력)의 뷰·테이블 — 앱 기동 시 있으면 정리한다.
#: (테이블 자동 생성은 "없는 것만 만들기"라 옛것을 지워주지 않는다)
CLEANUP_DDL: list[str] = [
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
    # 입출금 가능 여부에서 null 을 없앤다 — "확인 불가"도 False 로 통일한다.
    # 이미 쌓인 null 을 메운 뒤 NOT NULL 을 건다 (순서를 바꾸면 실패한다).
    "UPDATE market_snapshots SET deposit_enabled = FALSE "
    "WHERE deposit_enabled IS NULL",
    "UPDATE market_snapshots SET withdrawal_enabled = FALSE "
    "WHERE withdrawal_enabled IS NULL",
    "ALTER TABLE market_snapshots "
    "ALTER COLUMN deposit_enabled SET DEFAULT FALSE, "
    "ALTER COLUMN deposit_enabled SET NOT NULL",
    "ALTER TABLE market_snapshots "
    "ALTER COLUMN withdrawal_enabled SET DEFAULT FALSE, "
    "ALTER COLUMN withdrawal_enabled SET NOT NULL",
    # 옛 이름 정리 — 환율 테이블 fx_rate → usdkrw_rate.
    # (`fx` 는 해외 거래소를 가리키는 이름이라 환율에는 쓰지 않는다)
    # 뷰가 테이블을 참조하므로 뷰부터 지운다.
    "DROP VIEW IF EXISTS v_fx_rate",
    # 이 시점엔 create_all 이 usdkrw_rate 를 이미 만들어 둔 상태라 RENAME 은
    # 못 쓴다. 옛 테이블이 남아 있으면 한 행을 옮겨 담고 지운다.
    """
    DO $$
    BEGIN
        IF to_regclass('public.fx_rate') IS NOT NULL THEN
            INSERT INTO usdkrw_rate (id, rate, source_time, round_no, updated_at)
            SELECT id, rate, source_time, round_no, updated_at FROM fx_rate
            ON CONFLICT (id) DO NOTHING;
            DROP TABLE fx_rate;
        END IF;
    END $$
    """,
]
