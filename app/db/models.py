"""DB 테이블 정의 (SQLAlchemy ORM).

두 테이블뿐이다.

``market_snapshots`` — 거래소 × 코인 하나당 한 행
    코인 이름, 현재 가격(그 거래소 통화 그대로 — KRW 든 USDT 든 환산하지 않고 저장),
    asks/bids 호가 리스트, 입금·출금 가능 여부.
    환산이 필요한 계산은 조회 시점에 ``krw_rates`` 를 곱해서 한다.

``krw_rates`` — 국내 거래소당 한 행
    그 거래소 KRW-USDT 마켓의 환율 (USDT 1개당 원화).

호가 리스트는 ``[[가격, 잔량], ...]`` JSON 으로 저장한다. asks 는 가격 오름차순,
bids 는 내림차순 — 저장 시점에 이미 정렬되어 있어 조회 후 바로 훑을 수 있다.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, Float, String, func
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


class KrwRate(Base):
    """국내 거래소의 KRW-USDT 환율."""

    __tablename__ = "krw_rates"

    #: 국내 거래소 ID (upbit / bithumb)
    exchange: Mapped[str] = mapped_column(String(32), primary_key=True)
    #: USDT 1개당 원화 가격 (마지막 체결가)
    rate: Mapped[float] = mapped_column(Float)
    #: 원본 마켓 심볼 (KRW-USDT)
    native_symbol: Mapped[str] = mapped_column(String(64), default="")
    #: 거래소가 준 시세 시각 (epoch ms)
    price_timestamp: Mapped[int] = mapped_column(BigInteger, default=0)
    #: 이 행을 마지막으로 갱신한 시각
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
