"""사람이 읽는 DB 뷰 — GUI 뷰어(Beekeeper/Adminer/psql)용.

이력 테이블의 시각은 epoch 초(BIGINT)로 저장된다 — 연도까지 담긴 완전한
시각이지만 숫자 그대로는 읽기 어렵다. 그래서 **저장은 epoch 그대로 두고**,
연도가 포함된 KST 시각과 스케일이 풀린 실제 가격을 보여주는 읽기 전용 뷰를
같이 만들어 둔다. 뷰는 저장 공간을 차지하지 않고 원본 테이블을 실시간으로
비춰줄 뿐이다.

    v_price_points    스테이징 이벤트 — time_kst 로 "2026-08-12 09:00:00+09" 표기
    v_fx_points       환율 스테이징 이벤트
    v_price_chunks    청크 요약 — 시각·시/종/저/고가를 실제 단위로 풀어서
    v_fx_chunks       환율 청크 요약
    v_history_cursors 수집 커서
    v_fx_rate         라이브 환율 (고시 시각 포함)

가격 단위 규칙 (points/chunks 공통): ``exchange`` 가 단위를 결정한다 —
upbit 행은 KRW, binance 행은 USDT, 환율(fx_*)은 원/달러.

PostgreSQL 전용이다 (테스트용 SQLite 에서는 만들지 않는다).
"""

from __future__ import annotations

#: 앱 기동 시 CREATE OR REPLACE 되는 뷰 정의 목록.
#: 원본 테이블 스키마가 바뀌면 여기도 같이 고친다.
VIEW_DDL: list[str] = [
    # 스테이징 이벤트 — 변동 시각을 연도 포함 KST 로
    """
    CREATE OR REPLACE VIEW v_price_points AS
    SELECT exchange,
           base,
           to_timestamp(ts) AT TIME ZONE 'Asia/Seoul' AS time_kst,
           ts,
           price
    FROM price_points
    """,
    """
    CREATE OR REPLACE VIEW v_fx_points AS
    SELECT to_timestamp(ts) AT TIME ZONE 'Asia/Seoul' AS time_kst,
           ts,
           price AS usd_krw
    FROM fx_points
    """,
    # 청크 요약 — 압축 blob 은 빼고, 시각과 가격을 사람 단위로 풀어서
    """
    CREATE OR REPLACE VIEW v_price_chunks AS
    SELECT exchange,
           base,
           day,
           n_points,
           to_timestamp(first_ts) AT TIME ZONE 'Asia/Seoul' AS first_time_kst,
           to_timestamp(last_ts)  AT TIME ZONE 'Asia/Seoul' AS last_time_kst,
           round(first_price / power(10::numeric, price_scale), price_scale) AS open_price,
           round(last_price  / power(10::numeric, price_scale), price_scale) AS close_price,
           round(min_price   / power(10::numeric, price_scale), price_scale) AS low_price,
           round(max_price   / power(10::numeric, price_scale), price_scale) AS high_price,
           length(data) AS blob_bytes,
           codec,
           price_scale
    FROM price_chunks
    """,
    """
    CREATE OR REPLACE VIEW v_fx_chunks AS
    SELECT day,
           n_points,
           to_timestamp(first_ts) AT TIME ZONE 'Asia/Seoul' AS first_time_kst,
           to_timestamp(last_ts)  AT TIME ZONE 'Asia/Seoul' AS last_time_kst,
           round(first_price / power(10::numeric, price_scale), price_scale) AS open_rate,
           round(last_price  / power(10::numeric, price_scale), price_scale) AS close_rate,
           round(min_price   / power(10::numeric, price_scale), price_scale) AS low_rate,
           round(max_price   / power(10::numeric, price_scale), price_scale) AS high_rate,
           length(data) AS blob_bytes,
           codec,
           price_scale
    FROM fx_chunks
    """,
    """
    CREATE OR REPLACE VIEW v_history_cursors AS
    SELECT exchange,
           base,
           to_timestamp(last_ts) AT TIME ZONE 'Asia/Seoul' AS last_time_kst,
           last_ts,
           last_price,
           updated_at
    FROM history_cursors
    """,
    """
    CREATE OR REPLACE VIEW v_fx_rate AS
    SELECT rate AS usd_krw,
           round_no,
           to_timestamp(source_time) AT TIME ZONE 'Asia/Seoul' AS published_kst,
           updated_at
    FROM fx_rate
    """,
]
