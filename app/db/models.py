"""DB 테이블 정의 (SQLAlchemy ORM).

크게 세 묶음이다.

**라이브 (실시간 스프레드 창 담당)** — POST /refresh 가 갱신한다.

``market_snapshots`` — 거래소 × 코인 하나당 한 행
    코인 이름, 현재 가격(그 거래소 통화 그대로 — KRW 든 USDT 든 환산하지 않고 저장),
    asks/bids 호가 리스트, 입금·출금 가능 여부.
    **코인을 찾아 UPSERT 만 한다** — 지웠다 다시 만들지 않으며, 이번 수집에
    빠진 코인도 삭제하지 않는다 (신선도는 updated_at 으로 판별).

``usdkrw_rate`` — 국내 거래소당 한 행
    그 거래소 KRW-USDT 마켓의 최우선 호가 (ask/bid). 모든 원화 환산이
    이 값을 쓰며, **방향(김프/역프)에 따라 ask 와 bid 를 갈라 쓴다**.

**기록 (기록/통계 창 담당)** — 계속 쌓이는 append 전용.

``premium_archive`` — 김프/역프 기록 한 건당 한 행
    market_snapshots 가 업데이트될 때 코인·시각·김프·역프만 뽑아 저장하고,
    과거 구간은 대량 업데이트 스크립트(scripts/bulk_archive.py)가 거래소
    캔들로 계산해 채운다. 압축 없이 일반 행으로 저장한다 — 용량이 문제가
    되면 그때 압축을 다시 검토한다.

**플랫폼 상태**

``platform_status`` — 플랫폼(거래소)당 한 행
    마지막 수신 시각, 상장 마켓 수(현물/선물), 입출금 **조회** 실패 횟수와
    전체 업데이트 횟수. 실패율 = dw_fail_count / update_count.

``dw_fail_events`` — 입출금 조회 실패가 관측된 회차 한 건당 한 행
    dw_fail_count 가 +1 될 때 그 시각을 함께 남긴다 (수집 상태 창의
    결측 구간 표시용). 최근 24시간치만 유지 — 지난 행은 refresh 가
    돌 때마다 지운다.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Float,
    Integer,
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
    """거래소 × 코인 하나의 시세 스냅샷 (실시간 스프레드 창의 데이터)."""

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

    #: 입금 가능 여부 — **3-state 다.**
    #:
    #:     True  : 확인했고 열려 있음
    #:     False : 확인했고 막힘
    #:     None  : 확인 불가 — 키 없음 · API 실패 · 응답에 그 코인이 없음
    #:
    #: **None 을 "열림"으로 취급하지 말 것.** 모르는 경로를 옮길 수 있다고
    #: 말하는 셈이다. 판단을 `and` / `not` 으로 쓰면 None 이 falsy 라 자동으로
    #: 보수적이 된다.
    deposit_enabled: Mapped[bool | None] = mapped_column(default=None)
    #: 출금 가능 여부. 값의 뜻은 위와 같다.
    withdrawal_enabled: Mapped[bool | None] = mapped_column(default=None)

    #: 이 거래소가 이 코인에 지원하는 네트워크별 입출금 상태.
    #: ``[{"code": "BASENET", "name": "Base", "dep": true, "wd": true}, ...]``
    #:
    #: 코인 단위 deposit/withdrawal 만으로는 **실제로 옮길 수 있는지 알 수
    #: 없다** — 국내가 받는 망을 해외도 지원하고 그 망이 열려 있어야 한다.
    #: 거래소 쌍을 봐야 하는 판단이라 조회 시점(spread_service)에서 맞춘다.
    #: 비어 있으면 그 거래소가 망을 알려주지 않은 것이다 (= 확인 불가).
    networks: Mapped[list] = mapped_column(JsonList, default=list)

    #: 거래소가 준 시세 시각 (epoch ms). 없는 거래소는 수신 시각.
    price_timestamp: Mapped[int] = mapped_column(BigInteger, default=0)
    #: 이 행을 마지막으로 갱신한 시각 (DB 서버 시계)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class UsdKrwRate(Base):
    """환율 — **국내 거래소별** KRW-USDT 최우선 호가 한 쌍.

    원화 ↔ 달러 전환은 은행이 아니라 국내 거래소의 USDT 마켓에서 일어난다.
    그래서 환율도 그 마켓의 호가로 잡고, **방향별로 다른 값**을 쓴다.

        김프(원화 → USDT 매수) → ``ask``
        역프(USDT → 원화 매도) → ``bid``

    거래소마다 테더 프리미엄이 다르므로 행도 거래소마다 따로 둔다 (업비트 김프는
    업비트 USDT 호가로, 빗썸 김프는 빗썸 USDT 호가로 계산한다).
    """

    __tablename__ = "usdkrw_rate"

    #: 국내 거래소 ID (upbit / bithumb)
    exchange: Mapped[str] = mapped_column(String(32), primary_key=True)
    #: KRW-USDT 최우선 **매도**호가 — 원화로 USDT 를 살 때 체결되는 값
    ask: Mapped[float] = mapped_column(Float)
    #: KRW-USDT 최우선 **매수**호가 — USDT 를 원화로 팔 때 체결되는 값
    bid: Mapped[float] = mapped_column(Float)
    #: 이 행을 마지막으로 갱신한 시각
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PremiumArchive(Base):
    """김프/역프 기록 한 건 (기록/통계 창의 데이터).

    두 경로로 쌓인다.
        1. POST /refresh — market_snapshots 업데이트 직후, 스냅샷의 호가와
           통일 환율로 계산해 한 줄 추가 (체결측 호가 기준 — /spreads 와 동일식).
        2. scripts/bulk_archive.py — 아카이브의 첫/마지막 시각 밖 구간을
           거래소 캔들(업비트 초봉 · 바이낸스 1초봉)로 한 번에 계산해 채움
           (종가 기준 — 캔들에는 호가가 없다).

    시각은 절대 epoch 초로 저장한다 — 연도까지 담긴 완전한 시각이다.
    """

    __tablename__ = "premium_archive"

    #: 국내 거래소 (upbit / bithumb)
    dom: Mapped[str] = mapped_column(String(32), primary_key=True)
    #: 해외 거래소 (binance)
    fx: Mapped[str] = mapped_column(String(32), primary_key=True)
    #: 코인 이름 (BTC, ...)
    base: Mapped[str] = mapped_column(String(32), primary_key=True)
    #: 기록 시각 (절대 epoch 초)
    ts: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    #: 김프 % — 해외에서 사서 국내에 팔 때 수익률
    fwd: Mapped[float] = mapped_column(Float)
    #: 역프 % — 국내에서 사서 해외에 팔 때 수익률
    rev: Mapped[float] = mapped_column(Float)


class DwFailEvent(Base):
    """입출금 **조회** 실패가 관측된 refresh 회차 한 건 (결측 구간 재료).

    "조회 실패"는 거래소가 입출금 상태를 안 줬다는 뜻이다 (키 없음 · API
    장애 · 응답 누락). 코인이 막혀 있는 것은 실패가 아니다 — 그건 정상적으로
    관측된 결과다.

    platform_status 의 dw_fail_count 가 +1 되는 바로 그 갱신에서 같은
    트랜잭션으로 시각 한 줄을 남긴다. 조회 API 가 이 시각들을 이어 붙여
    "언제부터 언제까지 실패였는지" 구간으로 만들어 준다.

    보존 기간은 settings.dw_fail_retention_seconds (기본 24시간) —
    지난 행은 다음 refresh 가 함께 지우므로 별도 청소 잡이 없다.
    """

    __tablename__ = "dw_fail_events"

    #: 플랫폼(거래소) ID
    exchange: Mapped[str] = mapped_column(String(32), primary_key=True)
    #: 조회 실패가 관측된 수신 시각 (절대 epoch 초)
    ts: Mapped[int] = mapped_column(BigInteger, primary_key=True)


class PlatformStatus(Base):
    """플랫폼(거래소)당 한 행 — 수신 상태와 입출금 **조회** 실패율의 재료.

    POST /refresh 가 market_snapshots 를 업데이트한 뒤 같은 플랫폼의 행을
    함께 갱신한다:
        - ``last_received_ts`` 를 이번 수신 시각으로, ``update_count`` +1
        - 이번 업데이트에서 입출금 상태를 **못 받아온** 코인이 하나라도
          있었으면 ``dw_fail_count`` +1
    실패율 = dw_fail_count / update_count.

    막힌 코인 수를 세는 지표가 아니다. 그렇게 세면 코인이 늘수록 비율이
    1.0 에 수렴해 아무것도 말해주지 못한다.
    """

    __tablename__ = "platform_status"

    #: 플랫폼(거래소) ID
    exchange: Mapped[str] = mapped_column(String(32), primary_key=True)
    #: 마지막 수신 시각 (절대 epoch 초)
    last_received_ts: Mapped[int] = mapped_column(BigInteger, default=0)
    #: 상장 현물 마켓 수 (이번 수집에서 관측된 수)
    spot_market_count: Mapped[int] = mapped_column(Integer, default=0)
    #: 상장 선물 마켓 수 (선물이 없는 플랫폼은 0)
    futures_market_count: Mapped[int] = mapped_column(Integer, default=0)
    #: 입출금 조회 실패가 관측된 업데이트 횟수 (업데이트당 최대 +1)
    dw_fail_count: Mapped[int] = mapped_column(Integer, default=0)
    #: 전체 업데이트 횟수 (last_received_ts 갱신마다 +1)
    update_count: Mapped[int] = mapped_column(Integer, default=0)
    #: 이 행을 마지막으로 갱신한 시각
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
