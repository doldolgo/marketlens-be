"""거래소별 입출금 가능 여부 조회.

수집기(:mod:`app.services.collector_service`)가 refresh 때 호출해 결과를
``market_snapshots.deposit_enabled / withdrawal_enabled`` 에 저장한다.

    업비트   GET /v1/status/wallet          — JWT 인증 (UPBIT_API_KEY / UPBIT_SECRET_KEY)
    바이낸스 GET /sapi/v1/capital/config/getall — HMAC 서명 (BINANCE_API_KEY / BINANCE_SECRET_KEY)
    빗썸     GET /public/assetsstatus/multichain/ALL — 인증 불필요 (public)
             + GET /public/network-info  (net_type → 사람이 읽는 이름)

키가 없거나 호출이 실패하면 예외를 삼키지 않고 그대로 올린다. 실패 처리는
수집기가 한다 (해당 거래소의 입출금 값을 null 로 저장하고 경고를 남긴다).

한 코인이 여러 네트워크를 갖는 경우(예: USDT 의 TRX/ERC20), 코인 단위
``deposit`` / ``withdrawal`` 은 **하나라도 열려 있으면 가능**으로 판정한다.

다만 그 판정만으로는 **실제로 옮길 수 있는지 알 수 없다.** 국내 거래소가
지원하는 네트워크를 해외 거래소도 지원해야 하고, 그 네트워크가 열려 있어야
한다. 예를 들어 바이낸스 GRT 는 Arbitrum 출금이 열려 있어 "출금 가능"이지만
업비트는 Ethereum 으로만 받으므로 옮길 수 없다.

그래서 ``WalletStatus.networks`` 에 **네트워크별 상태를 그대로** 남긴다.
합치는 판단은 조회 시점(``spread_service``)이 거래소 쌍을 보고 한다 —
바이낸스 한 행이 업비트·빗썸 양쪽을 상대하는데 국내 네트워크가 서로 다를 수
있어 수집 시점에는 하나로 접을 수 없다.
"""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from dataclasses import dataclass
from urllib.parse import urlencode

import jwt

from app.core.config import settings
from app.core.errors import ExchangeAPIError
from app.core.http import get_client, record_call


@dataclass(frozen=True, slots=True)
class NetworkStatus:
    """코인 하나가 네트워크 하나에서 갖는 입출금 상태.

    ``code`` 는 거래소마다 제각각이다 (같은 Base 체인을 업비트는 ``BASENET``,
    빗썸은 ``BASE_ETH``, 바이낸스는 ``BASE`` 라 부른다). 그래서 거래소를
    가로질러 맞출 때는 ``name`` 을 함께 본다 — :mod:`app.exchanges.private.
    network_match` 참고.
    """

    code: str
    name: str
    deposit: bool
    withdrawal: bool


@dataclass(frozen=True, slots=True)
class WalletStatus:
    """코인 하나의 입출금 가능 여부.

    ``deposit`` / ``withdrawal`` 은 네트워크를 OR 로 합친 코인 단위 값이다.
    "옮길 수 있는가"를 판단하려면 ``networks`` 를 봐야 한다 (모듈 docstring).
    """

    deposit: bool
    withdrawal: bool
    #: 네트워크별 상태. 비어 있으면 그 거래소가 네트워크를 알려주지 않은 것이다.
    networks: tuple[NetworkStatus, ...] = ()


class MissingApiKeyError(Exception):
    """해당 거래소의 API 키가 설정되지 않았다."""


def _merge(
    statuses: dict[str, WalletStatus],
    currency: str,
    deposit: bool,
    withdrawal: bool,
    network: NetworkStatus | None = None,
) -> None:
    """같은 코인의 여러 네트워크를 OR 로 합친다.

    합친 값과 **별개로** 네트워크별 상태를 ``networks`` 에 그대로 쌓아 둔다.
    OR 로 접고 나면 "어느 망으로 열려 있는지"를 잃어버리는데, 그걸 잃으면
    실제로 옮길 수 있는지 판단할 수 없다.
    """
    prev = statuses.get(currency)
    networks = prev.networks if prev is not None else ()
    if network is not None:
        networks = (*networks, network)
    if prev is not None:
        deposit = deposit or prev.deposit
        withdrawal = withdrawal or prev.withdrawal
    statuses[currency] = WalletStatus(
        deposit=deposit, withdrawal=withdrawal, networks=networks
    )


# ----------------------------------------------------------------------
# 업비트
# ----------------------------------------------------------------------


async def fetch_upbit_wallet_status() -> dict[str, WalletStatus]:
    """업비트 전 코인의 입출금 가능 여부.

    ``wallet_state`` 값 해석 (업비트 문서 기준):
        working       입출금 모두 가능
        withdraw_only 출금만 가능
        deposit_only  입금만 가능
        paused        입출금 모두 중단
        unsupported   입출금 미지원
    """
    if not settings.upbit_api_key or not settings.upbit_secret_key:
        raise MissingApiKeyError("UPBIT_API_KEY / UPBIT_SECRET_KEY 가 비어 있습니다.")

    # 쿼리 파라미터가 없는 요청은 access_key + nonce 만 담은 JWT 면 된다.
    token = jwt.encode(
        {"access_key": settings.upbit_api_key, "nonce": str(uuid.uuid4())},
        settings.upbit_secret_key,
        algorithm="HS256",
    )

    record_call("upbit")
    response = await get_client().get(
        f"{settings.upbit_base_url}/v1/status/wallet",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    )
    if response.status_code != 200:
        raise ExchangeAPIError(
            f"업비트 지갑 상태 API 가 {response.status_code} 를 반환했습니다.",
            detail={"exchange": "upbit", "body": response.text[:500]},
        )

    statuses: dict[str, WalletStatus] = {}
    for row in response.json():
        currency = row.get("currency")
        state = row.get("wallet_state")
        if not currency or not state:
            continue
        deposit = state in ("working", "deposit_only")
        withdrawal = state in ("working", "withdraw_only")
        net_code = (row.get("net_type") or "").upper()
        _merge(
            statuses,
            currency.upper(),
            deposit=deposit,
            withdrawal=withdrawal,
            network=NetworkStatus(
                code=net_code,
                # network_name 이 없으면 코드로 대신한다 — 이름이 없다고
                # 네트워크를 통째로 버리면 판정 근거가 사라진다
                name=row.get("network_name") or net_code,
                deposit=deposit,
                withdrawal=withdrawal,
            )
            if net_code
            else None,
        )
    return statuses


# ----------------------------------------------------------------------
# 바이낸스
# ----------------------------------------------------------------------


async def fetch_binance_wallet_status() -> dict[str, WalletStatus]:
    """바이낸스 전 코인의 입출금 가능 여부.

    코인 레벨의 ``depositAllEnable`` / ``withdrawAllEnable`` 은 **모든** 네트워크가
    열려 있어야 True 라 지나치게 엄격하다. 네트워크 목록을 직접 보고
    하나라도 열려 있으면 가능으로 판정한다.
    """
    if not settings.binance_api_key or not settings.binance_secret_key:
        raise MissingApiKeyError(
            "BINANCE_API_KEY / BINANCE_SECRET_KEY 가 비어 있습니다."
        )

    query = urlencode({"timestamp": int(time.time() * 1000), "recvWindow": 10_000})
    signature = hmac.new(
        settings.binance_secret_key.encode(), query.encode(), hashlib.sha256
    ).hexdigest()

    record_call("binance")
    response = await get_client().get(
        f"{settings.binance_spot_base_url}/sapi/v1/capital/config/getall"
        f"?{query}&signature={signature}",
        headers={"X-MBX-APIKEY": settings.binance_api_key},
        timeout=10.0,
    )
    if response.status_code != 200:
        raise ExchangeAPIError(
            f"바이낸스 지갑 상태 API 가 {response.status_code} 를 반환했습니다.",
            detail={"exchange": "binance", "body": response.text[:500]},
        )

    statuses: dict[str, WalletStatus] = {}
    for row in response.json():
        coin = row.get("coin")
        networks = row.get("networkList") or []
        if not coin or not networks:
            continue
        statuses[coin.upper()] = WalletStatus(
            deposit=any(n.get("depositEnable") for n in networks),
            withdrawal=any(n.get("withdrawEnable") for n in networks),
            networks=tuple(
                NetworkStatus(
                    code=(n.get("network") or "").upper(),
                    name=n.get("name") or (n.get("network") or ""),
                    deposit=bool(n.get("depositEnable")),
                    withdrawal=bool(n.get("withdrawEnable")),
                )
                for n in networks
                if n.get("network")
            ),
        )
    return statuses


# ----------------------------------------------------------------------
# 빗썸
# ----------------------------------------------------------------------


async def _bithumb_network_names() -> dict[str, str]:
    """빗썸 net_type → 사람이 읽는 이름. 실패해도 판정을 막지 않는다.

    이름이 없으면 코드로 대신한다 — 코드끼리 맞는 경우가 대부분이라
    이름 사전이 잠깐 없다고 네트워크 판정을 통째로 포기할 이유는 없다.
    """
    try:
        response = await get_client().get(
            f"{settings.bithumb_base_url}/public/network-info", timeout=10.0
        )
        body = response.json()
        if body.get("status") != "0000":
            return {}
        return {
            row["net_type"].upper(): row.get("net_name") or row["net_type"]
            for row in body.get("data") or []
            if row.get("net_type")
        }
    except Exception:  # noqa: BLE001 — 이름 사전은 있으면 좋은 정도다
        return {}


async def fetch_bithumb_wallet_status() -> dict[str, WalletStatus]:
    """빗썸 전 코인의 입출금 가능 여부 — public API 라 키가 필요 없다.

    ``/public/assetsstatus/ALL`` 은 코인 단위라 어느 네트워크로 열려 있는지
    알 수 없다. **네트워크 단위**로 주는 ``multichain`` 쪽을 쓴다:

        {"status": "0000", "data": [{"currency": "ETH", "net_type": "ARB_ETH",
         "deposit_status": 1, "withdrawal_status": 1}, ...]}   (1 = 가능)
    """
    record_call("bithumb")
    response = await get_client().get(
        f"{settings.bithumb_base_url}/public/assetsstatus/multichain/ALL",
        timeout=10.0,
    )
    if response.status_code != 200:
        raise ExchangeAPIError(
            f"빗썸 자산 상태 API 가 {response.status_code} 를 반환했습니다.",
            detail={"exchange": "bithumb", "body": response.text[:500]},
        )

    body = response.json()
    if body.get("status") != "0000" or not isinstance(body.get("data"), list):
        raise ExchangeAPIError(
            "빗썸 자산 상태 응답 형식이 올바르지 않습니다.",
            detail={"exchange": "bithumb", "body": str(body)[:500]},
        )

    names = await _bithumb_network_names()

    statuses: dict[str, WalletStatus] = {}
    for row in body["data"]:
        currency = row.get("currency")
        if not currency:
            continue
        deposit = row.get("deposit_status") == 1
        withdrawal = row.get("withdrawal_status") == 1
        net_code = (row.get("net_type") or "").upper()
        _merge(
            statuses,
            currency.upper(),
            deposit=deposit,
            withdrawal=withdrawal,
            network=NetworkStatus(
                code=net_code,
                name=names.get(net_code, net_code),
                deposit=deposit,
                withdrawal=withdrawal,
            )
            if net_code
            else None,
        )
    return statuses
