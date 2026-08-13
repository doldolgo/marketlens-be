"""DB 테이블 정의 (SQLAlchemy ORM).

크게 두 묶음이다.

**현재 상태 (라이브)** — POST /refresh 가 매번 통째로 갱신한다.

``market_snapshots`` — 거래소 × 코인 하나당 한 행
    코인 이름, 현재 가격(그 거래소 통화 그대로 — KRW 든 USDT 든 환산하지 않고 저장),
    asks/bids 호가 리스트, 입금·출금 가능 여부.
    환산이 필요한 계산은 조회 시점에 ``fx_rate`` 를 곱해서 한다.

``fx_rate`` — 단 한 행
    하나은행 고시 USD/KRW **매매기준율** (최신 고시).
    예전에는 국내 거래소별 KRW-USDT 시세를 환율로 썼지만(``krw_rates``),
    지금은 모든 계산이 이 은행 고시 환율 하나로 통일됐다.

**가격 변동 이력 (히스토리)** — 김프/역프 통계의 원재료.

``price_chunks`` — (거래소 × 코인 × UTC 하루) 당 한 행
    그 날의 가격 **변동 이벤트**(변한 순간의 절대 epoch 초 + 가격)를
    :mod:`app.history.codec` 으로 압축한 bytea 블롭. 나머지 컬럼은
    블롭을 풀지 않고도 범위를 거를 수 있게 하는 메타데이터다.

``price_points`` — 아직 하루가 끝나지 않아 압축 전인 변동 이벤트 (스테이징)
    자정(UTC)이 지나 하루가 완결되면 팩킹 과정이 청크로 옮기고 지운다.
    가격은 십진 문자열 그대로 저장한다 — float 반올림을 원천 차단하기 위해서다.

``fx_chunks`` / ``fx_points`` — 환율(USD/KRW 매매기준율)의 같은 구조.
    환율은 거래소 개념이 없으므로 exchange/base 컬럼이 없다.

``history_cursors`` — 시리즈별 증분 수집 위치 (마지막으로 반영한 시각·가격)
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Date,
    DateTime,
    Float,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

#: PostgreSQL 에서는 JSONB, 그 외(테스트용 SQLite 등)에서는 일반 JSON 을 쓴다.
JsonList = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


class MarketSnapshot(Base):
    """거래소 × 코인 하나의 시세 스냅샷."""

    __tablename__ = "market_snapshots"

    #: 거래소 ID (upbit / bithumb / binance)
    exchange: Mapped[str] = mapped_column(String(32), primary_key=True)
    #: 코인 이름 (BTC, ETH, ...)
    base: Mapped[str] = mapped_column(String(32), primary_key=True)

    #: 거래소 원본 심볼 (KRW-BTC, BTCUSDT)
    native_symbol: Mapped[str] = mapped_column(String(64))
    #: 가격 통화 (KRW / USDT). 가격·호가를 환산 없이 그대로 저장하므로 단위를 기록한다.
    quote: Mapped[str] = mapped_column(String(16))

    #: 현재 가격 (마지막 체결가, quote 통화 그대로)
    price: Mapped[float] = mapped_column(Float)

    #: 매도 호가 [[가격, 잔량], ...] 가격 오름차순
    asks: Mapped[list] = mapped_column(JsonList, default=list)
    #: 매수 호가 [[가격, 잔량], ...] 가격 내림차순
    bids: Mapped[list] = mapped_column(JsonList, default=list)

    #: 입금 가능 여부. 확인할 수 없으면(키 없음 등) None.
    deposit_enabled: Mapped[bool | None] = mapped_column(default=None)
    #: 출금 가능 여부. 확인할 수 없으면 None.
    withdrawal_enabled: Mapped[bool | None] = mapped_column(default=None)

    #: 거래소가 준 시세 시각 (epoch ms). 없는 거래소는 수신 시각.
    price_timestamp: Mapped[int] = mapped_column(BigInteger, default=0)
    #: 이 행을 마지막으로 갱신한 시각 (DB 서버 시계)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class FxRate(Base):
    """통일 환율 — 하나은행 고시 USD/KRW 매매기준율의 최신 값 **한 행**.

    거래소별 환율 개념을 없앴다. 김프/역프/차익 계산 전부가 이 행 하나를 쓴다.
    이력은 ``fx_chunks`` / ``fx_points`` 에 따로 쌓인다 — 이 행은 "지금 값" 전용.
    """

    __tablename__ = "fx_rate"

    #: 항상 1. 단일 행을 강제하기 위한 고정 PK.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    #: USD 1달러당 원화 (하나은행 매매기준율)
    rate: Mapped[float] = mapped_column(Float)
    #: 은행이 이 환율을 고시한 시각 (epoch 초)
    source_time: Mapped[int] = mapped_column(BigInteger, default=0)
    #: 당일 고시 회차 (하루 1,300~2,000회 갱신된다)
    round_no: Mapped[int] = mapped_column(Integer, default=0)
    #: 이 행을 마지막으로 갱신한 시각
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# ----------------------------------------------------------------------
# 가격 변동 이력
# ----------------------------------------------------------------------


class PriceChunk(Base):
    """거래소 × 코인 × UTC 하루의 가격 변동 이벤트 압축 블롭.

    블롭 안에는 (절대 epoch 초, 스케일 정수 가격) 열이 들어 있다.
    포맷은 :mod:`app.history.codec` 참고. 나머지 컬럼은 블롭을 풀지 않고
    기간·가격 범위를 거르기 위한 요약값이다.
    """

    __tablename__ = "price_chunks"

    #: 거래소 ID (upbit / binance)
    exchange: Mapped[str] = mapped_column(String(32), primary_key=True)
    #: 코인 이름 (BTC, ETH, ...)
    base: Mapped[str] = mapped_column(String(32), primary_key=True)
    #: 이 청크가 담는 UTC 날짜
    day: Mapped[date] = mapped_column(Date, primary_key=True)

    #: 블롭의 코덱 버전 (codec.CODEC_VERSION)
    codec: Mapped[int] = mapped_column(SmallInteger)
    #: 가격 = 정수 ÷ 10^price_scale. 청크마다 데이터에 맞춰 정해진다.
    price_scale: Mapped[int] = mapped_column(SmallInteger)
    #: 포인트(변동 이벤트) 수
    n_points: Mapped[int] = mapped_column(Integer)

    #: 첫/마지막 이벤트 시각 (epoch 초). last_price 는 다음 날 변동 판정의 씨앗.
    first_ts: Mapped[int] = mapped_column(BigInteger)
    last_ts: Mapped[int] = mapped_column(BigInteger)
    #: 첫/마지막/최저/최고 가격 (스케일 정수) — 블롭 없이 요약 조회용
    first_price: Mapped[int] = mapped_column(BigInteger)
    last_price: Mapped[int] = mapped_column(BigInteger)
    min_price: Mapped[int] = mapped_column(BigInteger)
    max_price: Mapped[int] = mapped_column(BigInteger)

    #: 압축 블롭. 앱이 이미 zstd 로 압축했으므로 Postgres 가 또 압축하지 않게
    #: 배포 시 `ALTER TABLE price_chunks ALTER COLUMN data SET STORAGE EXTERNAL` 권장.
    data: Mapped[bytes] = mapped_column(LargeBinary)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PricePoint(Base):
    """아직 압축 전인 가격 변동 이벤트 한 건 (스테이징).

    오늘(UTC) 데이터가 여기 쌓이고, 하루가 완결되면 팩킹이 청크로 옮긴다.
    가격은 거래소 API 가 준 십진 표기 그대로의 문자열이다 — 정확한 값 보존이
    목적이고, 산술은 팩킹/조회 때 Decimal 로 한다.
    """

    __tablename__ = "price_points"

    exchange: Mapped[str] = mapped_column(String(32), primary_key=True)
    base: Mapped[str] = mapped_column(String(32), primary_key=True)
    #: 변동 시각 (절대 epoch 초)
    ts: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    #: 변동 후 가격 (십진 문자열, 예: "90191000" / "1518.4")
    price: Mapped[str] = mapped_column(String(40))


class FxChunk(Base):
    """환율(USD/KRW 매매기준율) 변동 이벤트의 UTC 하루 압축 블롭.

    구조는 :class:`PriceChunk` 와 같고, 환율은 거래소·코인 개념이 없으므로
    날짜만이 키다.
    """

    __tablename__ = "fx_chunks"

    day: Mapped[date] = mapped_column(Date, primary_key=True)

    codec: Mapped[int] = mapped_column(SmallInteger)
    price_scale: Mapped[int] = mapped_column(SmallInteger)
    n_points: Mapped[int] = mapped_column(Integer)
    first_ts: Mapped[int] = mapped_column(BigInteger)
    last_ts: Mapped[int] = mapped_column(BigInteger)
    first_price: Mapped[int] = mapped_column(BigInteger)
    last_price: Mapped[int] = mapped_column(BigInteger)
    min_price: Mapped[int] = mapped_column(BigInteger)
    max_price: Mapped[int] = mapped_column(BigInteger)
    data: Mapped[bytes] = mapped_column(LargeBinary)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class FxPoint(Base):
    """아직 압축 전인 환율 변동 이벤트 한 건 (스테이징)."""

    __tablename__ = "fx_points"

    #: 고시 시각 (절대 epoch 초)
    ts: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    #: 매매기준율 (십진 문자열, 예: "1518.4")
    price: Mapped[str] = mapped_column(String(40))


class HistoryCursor(Base):
    """시리즈별 증분 수집 커서 — 어디까지 반영했는지.

    ``last_ts`` 이후의 데이터만 새로 받아오면 되고, ``last_price`` 는
    "가격이 변했는가" 판정의 기준값(직전 가격)이다. 환율 시리즈는
    exchange='fx', base='USD' 로 같은 테이블을 쓴다.
    """

    __tablename__ = "history_cursors"

    exchange: Mapped[str] = mapped_column(String(32), primary_key=True)
    base: Mapped[str] = mapped_column(String(32), primary_key=True)
    #: 마지막으로 반영한 이벤트 시각 (epoch 초)
    last_ts: Mapped[int] = mapped_column(BigInteger)
    #: 마지막으로 관측한 가격 (십진 문자열) — 다음 변동 판정의 씨앗
    last_price: Mapped[str] = mapped_column(String(40))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
