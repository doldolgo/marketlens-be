"""거래소별 입출금 가능 여부 조회.

수집기(:mod:`app.services.collector_service`)가 refresh 때 호출해 결과를
``market_snapshots.deposit_enabled / withdrawal_enabled`` 에 저장한다.

    업비트   GET /v1/status/wallet          — JWT 인증 (UPBIT_API_KEY / UPBIT_SECRET_KEY)
    바이낸스 GET /sapi/v1/capital/config/getall — HMAC 서명 (BINANCE_API_KEY / BINANCE_SECRET_KEY)
    빗썸     GET /public/assetsstatus/ALL   — 인증 불필요 (public)

키가 없거나 호출이 실패하면 예외를 삼키지 않고 그대로 올린다. 실패 처리는
수집기가 한다 (해당 거래소의 입출금 값을 null 로 저장하고 경고를 남긴다).

한 코인이 여러 네트워크를 갖는 경우(예: USDT 의 TRX/ERC20), **하나라도 열려
있으면 가능**으로 판정한다.
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
class WalletStatus:
    """코인 하나의 입출금 가능 여부."""

    deposit: bool
    withdrawal: bool


class MissingApiKeyError(Exception):
    """해당 거래소의 API 키가 설정되지 않았다."""


def _merge(
    statuses: dict[str, WalletStatus], currency: str, deposit: bool, withdrawal: bool
) -> None:
    """같은 코인의 여러 네트워크를 OR 로 합친다."""
    prev = statuses.get(currency)
    if prev is not None:
        deposit = deposit or prev.deposit
        withdrawal = withdrawal or prev.withdrawal
    statuses[currency] = WalletStatus(deposit=deposit, withdrawal=withdrawal)


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
        _merge(
            statuses,
            currency.upper(),
            deposit=state in ("working", "deposit_only"),
            withdrawal=state in ("working", "withdraw_only"),
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
        )
    return statuses


# ----------------------------------------------------------------------
# 빗썸
# ----------------------------------------------------------------------


async def fetch_bithumb_wallet_status() -> dict[str, WalletStatus]:
    """빗썸 전 코인의 입출금 가능 여부 — public API 라 키가 필요 없다.

    응답: ``{"status": "0000", "data": {"BTC": {"deposit_status": 1,
    "withdrawal_status": 1}, ...}}`` (1 = 가능, 0 = 중단)
    """
    record_call("bithumb")
    response = await get_client().get(
        f"{settings.bithumb_base_url}/public/assetsstatus/ALL", timeout=10.0
    )
    if response.status_code != 200:
        raise ExchangeAPIError(
            f"빗썸 자산 상태 API 가 {response.status_code} 를 반환했습니다.",
            detail={"exchange": "bithumb", "body": response.text[:500]},
        )

    body = response.json()
    if body.get("status") != "0000" or not isinstance(body.get("data"), dict):
        raise ExchangeAPIError(
            "빗썸 자산 상태 응답 형식이 올바르지 않습니다.",
            detail={"exchange": "bithumb", "body": str(body)[:500]},
        )

    statuses: dict[str, WalletStatus] = {}
    for currency, row in body["data"].items():
        if not isinstance(row, dict):
            continue
        statuses[currency.upper()] = WalletStatus(
            deposit=row.get("deposit_status") == 1,
            withdrawal=row.get("withdrawal_status") == 1,
        )
    return statuses
