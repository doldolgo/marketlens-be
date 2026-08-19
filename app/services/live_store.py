"""메모리 안의 최신 시세 — 조회 API 가 읽는 진실.

수집기가 사이클마다 통째로 교체하고, 조회 서비스들이 여기서 읽는다.
DB(``market_snapshots``)는 기록·재기동 복구용으로만 남는다.

**왜 메모리인가.** 수집 사이클 2.6초 중 2.2초(85%)가 DB 쓰기였다. 조회가
DB 를 보고 있었기 때문에 "코인 하나마다 커밋"으로 최신값을 빨리 노출해야
했고, 그 커밋 245회가 사이클을 잡아먹었다. 조회가 메모리를 보면 이
트레이드오프 자체가 없어진다.

**설계 원칙 — repository 와 같은 시그니처.** 아래 읽기 함수들은
:mod:`app.db.repository` 의 동명 함수와 인자·반환 모양이 같다 (``session``
인자만 없다). 그래서 조회 서비스는 "데이터를 어디서 받는지"만 바뀌고
계산 로직은 손대지 않는다. :class:`LiveSnapshot` 도 같은 이유로
:class:`app.db.models.MarketSnapshot` 과 속성 이름이 같다 —
``levels_from_json`` / ``orderbook_from_snapshot`` 이 속성만 읽으므로
그대로 재사용된다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import MarketDataNotFoundError
from app.db import repository
from app.db.models import MarketSnapshot, UsdKrwRate


@dataclass(slots=True)
class LiveSnapshot:
    """메모리에 들고 있는 스냅샷 한 행.

    :class:`app.db.models.MarketSnapshot` 과 **같은 속성 이름**을 갖는다.
    ``repository.levels_from_json`` / ``orderbook_from_snapshot`` 이 속성만
    읽으므로 그대로 재사용된다 — 이 이름들을 바꾸면 안 된다.
    """

    exchange: str
    base: str
    native_symbol: str
    quote: str
    price: float
    asks: list[list[float]] = field(default_factory=list)
    bids: list[list[float]] = field(default_factory=list)
    #: 입출금 가능 여부는 3-state 다 — True=열림 / False=막힘 / None=확인 불가.
    #: (`app.db.models.MarketSnapshot` 의 같은 이름 필드와 뜻이 같다)
    deposit_enabled: bool | None = None
    withdrawal_enabled: bool | None = None
    #: 거래소가 준 시세 시각 (epoch ms)
    price_timestamp: int = 0
    #: 이 행을 메모리에 넣은 시각. **timezone-aware UTC 로만 넣는다** —
    #: naive 로 두면 spread_service._age_seconds() 가 UTC 로 간주해 보정하는
    #: 분기를 타게 되고, DB(PostgreSQL, aware)와 표현이 갈린다.
    updated_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


@dataclass(slots=True)
class LiveRate:
    """메모리에 들고 있는 통일 환율. :class:`app.db.models.UsdKrwRate` 와 같은 속성."""

    rate: float
    source_time: int = 0
    round_no: int = 0
    updated_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


#: 조회 서비스가 다루는 스냅샷. 메모리든 DB 든 속성이 같아 계산 코드는 구분하지 않는다.
AnySnapshot = LiveSnapshot | MarketSnapshot
#: 조회 서비스가 다루는 통일 환율. 위와 같은 이유로 둘을 구분하지 않는다.
AnyRate = LiveRate | UsdKrwRate


class LiveStore:
    """수집 사이클이 통째로 교체하는, 프로세스 메모리 안의 최신 시세."""

    def __init__(self) -> None:
        #: (거래소, 코인) → 스냅샷
        self._snapshots: dict[tuple[str, str], LiveSnapshot] = {}
        self._rate: LiveRate | None = None
        self._received_at: float | None = None

    # ------------------------------------------------------------------
    # 쓰기 — 수집기 전용
    # ------------------------------------------------------------------

    def replace(
        self,
        snapshots: list[LiveSnapshot],
        rate: LiveRate | None,
        received_at: float,
    ) -> None:
        """이번 사이클 결과로 **통째로** 교체한다.

        코인별 부분 갱신을 하지 않는 이유 — 사이클 중간 상태가 노출되면
        코인마다 시각이 어긋난 값을 비교하게 된다. 통째로 바꾸므로 상장폐지
        코인은 별도 삭제 로직 없이 자동으로 빠진다.

        Args:
            snapshots: 이번 사이클이 조립한 스냅샷 전체.
            rate: 통일 환율. ``None`` 이면 직전 값을 유지한다 (환율은 분
                단위로 급변하지 않아 낡은 값이 없는 것보다 낫다).
            received_at: 거래소 응답 조립이 끝난 시각 (epoch 초).
        """
        self._snapshots = {(s.exchange, s.base): s for s in snapshots}
        if rate is not None:
            self._rate = rate
        self._received_at = received_at

    def clear(self) -> None:
        """메모리를 비운다 (테스트에서 콜드스타트 상태를 만들 때 쓴다)."""
        self._snapshots = {}
        self._rate = None
        self._received_at = None

    # ------------------------------------------------------------------
    # 읽기 — repository 와 같은 시그니처 (session 인자만 없다)
    # ------------------------------------------------------------------

    def get_snapshots(
        self, *, exchange: str | None = None, base: str | None = None
    ) -> list[LiveSnapshot]:
        """스냅샷을 조건으로 조회한다. 조건이 없으면 전체."""
        wanted_base = base.upper() if base is not None else None
        return [
            s
            for s in self._snapshots.values()
            if (exchange is None or s.exchange == exchange)
            and (wanted_base is None or s.base == wanted_base)
        ]

    def get_snapshot(self, exchange: str, base: str) -> LiveSnapshot | None:
        """거래소 × 코인 하나의 스냅샷."""
        return self._snapshots.get((exchange, base.upper()))

    def require_snapshot(self, exchange: str, base: str) -> LiveSnapshot:
        """스냅샷을 가져오되, 없으면 404 성격의 도메인 예외를 던진다."""
        snap = self.get_snapshot(exchange, base)
        if snap is None:
            raise MarketDataNotFoundError(
                f"메모리에 {exchange} 거래소의 {base.upper()} 스냅샷이 없습니다. "
                "POST /refresh 로 데이터를 수집했는지, 해당 거래소에 상장된 코인인지 "
                "확인하세요.",
                detail={"exchange": exchange, "base": base.upper()},
            )
        return snap

    def get_usdkrw_rate(self) -> LiveRate | None:
        """통일 환율(USD/KRW 매매기준율). 아직 수집 전이면 None."""
        return self._rate

    def require_usdkrw_rate(self) -> LiveRate:
        """통일 환율을 가져오되, 없거나 0 이하면 도메인 예외를 던진다."""
        rate = self._rate
        if rate is None or rate.rate <= 0:
            raise MarketDataNotFoundError(
                "메모리에 USD/KRW 환율이 없습니다. "
                "POST /refresh 로 수집했는지 확인하세요.",
            )
        return rate

    @property
    def received_at(self) -> float | None:
        """마지막 수집 사이클이 거래소 응답을 받은 시각 (epoch 초)."""
        return self._received_at

    def is_empty(self) -> bool:
        """아직 한 사이클도 못 돌았는지 (앱 재기동 직후 콜드스타트)."""
        return not self._snapshots


#: 프로세스 하나에 하나 (collector_service 와 같은 관행).
live_store = LiveStore()


# ----------------------------------------------------------------------
# 콜드스타트 폴백 — 메모리가 비면 DB 를 읽는다
# ----------------------------------------------------------------------
#
# 앱 재기동 직후 첫 수집 사이클 전까지 메모리가 비어 있다. 폴백이 없으면
# 그 구간에 모든 조회 API 가 404 를 뱉는다. 서비스 7곳에 같은 분기를
# 복붙하지 않도록 여기 한 곳에 모은다.


async def snapshots_or_db(
    session: AsyncSession,
    *,
    exchange: str | None = None,
    base: str | None = None,
) -> list[AnySnapshot]:
    """메모리 스냅샷. 비어 있으면 DB 로 폴백한다."""
    if live_store.is_empty():
        return await repository.get_snapshots(session, exchange=exchange, base=base)
    return live_store.get_snapshots(exchange=exchange, base=base)


async def snapshot_or_db(
    session: AsyncSession, exchange: str, base: str
) -> AnySnapshot | None:
    """거래소 × 코인 하나. 메모리가 비어 있으면 DB 로 폴백한다."""
    if live_store.is_empty():
        return await repository.get_snapshot(session, exchange, base)
    return live_store.get_snapshot(exchange, base)


async def require_snapshot_or_db(
    session: AsyncSession, exchange: str, base: str
) -> AnySnapshot:
    """거래소 × 코인 하나. 없으면 :class:`MarketDataNotFoundError`."""
    if live_store.is_empty():
        return await repository.require_snapshot(session, exchange, base)
    return live_store.require_snapshot(exchange, base)


async def usdkrw_rate_or_db(session: AsyncSession) -> AnyRate | None:
    """통일 환율. 메모리에 없으면 DB 로 폴백한다. 양쪽 다 없으면 None.

    스냅샷과 달리 ``is_empty()`` 가 아니라 **환율 유무**로 판단한다 — 첫
    사이클에서 하나은행 고시만 실패하면 스냅샷은 있는데 환율만 없는 상태가
    되고, 그때 DB 의 마지막 환율을 쓰는 편이 404 보다 낫다.
    """
    rate = live_store.get_usdkrw_rate()
    if rate is None:
        return await repository.get_usdkrw_rate(session)
    return rate


async def require_usdkrw_rate_or_db(session: AsyncSession) -> AnyRate:
    """통일 환율. 없거나 0 이하면 :class:`MarketDataNotFoundError`."""
    rate = live_store.get_usdkrw_rate()
    if rate is None or rate.rate <= 0:
        return await repository.require_usdkrw_rate(session)
    return rate


def received_at_ms(snapshots: Sequence[AnySnapshot]) -> int | None:
    """이 응답에 담긴 데이터를 **거래소에서 받은** 시각 (epoch ms).

    메모리 경로면 마지막 수집 사이클의 수신 시각이다. DB 폴백 중이면 쓰인
    스냅샷의 ``updated_at`` 중 **가장 오래된 것** — 여러 시각이 섞인 응답에서
    보장할 수 있는 최신성은 가장 낡은 쪽이기 때문이다.

    응답 전체의 시각이라 코인별 ``data_updated_at`` 과 다르고, 서버가 응답을
    만든 ``fetched_at`` 과도 다르다.
    """
    at = live_store.received_at
    if at is not None and not live_store.is_empty():
        return int(at * 1000)

    stamps = [s.updated_at for s in snapshots if s.updated_at is not None]
    if not stamps:
        return None
    oldest = min(stamps)
    if oldest.tzinfo is None:
        # SQLite(테스트)는 naive UTC 를 준다 — 로컬 시간으로 읽히면 시차만큼 틀어진다.
        oldest = oldest.replace(tzinfo=timezone.utc)
    return int(oldest.timestamp() * 1000)
