"""거래소 간 네트워크 맞추기 — "이 코인을 실제로 옮길 수 있는가".

국내 거래소가 지원하는 네트워크를 해외 거래소도 지원해야 하고, **그
네트워크가** 열려 있어야 옮길 수 있다. 코인 단위 "입출금 가능"만 보면
틀린다 — 바이낸스 GRT 는 Arbitrum 출금이 열려 "가능"이지만 업비트는
Ethereum 으로만 받으므로 옮길 수 없다.

**국내 거래소를 기준으로 잡는다.** 업비트·빗썸은 코인의 98% 가 네트워크
하나뿐이라(업비트 379/384, 빗썸 503/511) 사실상 국내 망이 곧 제약이다.

거래소마다 이름이 제각각인 게 문제다:

    같은 Base 체인   업비트 BASENET   빗썸 BASE_ETH   바이낸스 BASE
    같은 Polkadot    업비트 DOT       빗썸 DOT        바이낸스 STATEMINT
                     "AssetHub Polkadot"             "Asset Hub Polkadot"

그래서 코드 → 이름 정규화 순으로 맞춘다. 그래도 안 맞으면 **추측하지
않는다.** 판정을 셋으로 나누는 이유가 그것이다:

    MATCHED  해당 네트워크를 찾았다 — 그 망의 입출금 상태를 쓴다
    ABSENT   이름이 조금도 안 겹친다 — 다른 체인으로 본다 (못 옮김)
    UNKNOWN  겹치긴 하는데 같은 망이라 단정할 수 없다 — 확인 불가

UNKNOWN 을 "옮길 수 있음"으로 접으면 안 된다. 오탐(같은 망을 다르다고
함)은 기회를 놓치는 데 그치지만, 미탐(다른 망을 같다고 함)은 못 옮기는
경로를 옮길 수 있다고 말하는 것이라 돈이 나간다. 그래서 애매하면 UNKNOWN.

예: 업비트 "Sei"(네이티브)와 바이낸스 "SEIEVM" 은 토큰이 겹치지만 서로 다른
주소 체계다. 부분 일치로 맞춰버리면 못 옮기는 코인이 "가능"으로 돌아온다.
"""

from __future__ import annotations

import re
from enum import Enum

from app.exchanges.private.wallet_status import NetworkStatus

#: 이름에 붙어도 체인 정체를 바꾸지 않는 장식어.
#: ("Polygon" ↔ "Polygon POS", "Sonic" ↔ "Sonic Network")
_STOPWORDS = frozenset(
    {"network", "networks", "chain", "mainnet", "protocol", "pos", "token", "coin"}
)

#: 같은 체인을 가리키는 다른 표기. 토큰 하나 단위로 바꾼다.
#: ("Avalanche C-Chain" ↔ "AVAX C-Chain")
_TOKEN_ALIAS = {
    "avax": "avalanche",
    "eth": "ethereum",
    "btc": "bitcoin",
    "matic": "polygon",
    "pol": "polygon",
    "sol": "solana",
    "trx": "tron",
    "arb": "arbitrum",
    "op": "optimism",
}


#: 자동으로는 안 맞지만 **확인 결과 같은 체인**인 쌍. 손으로 등록한다.
#: 자동 규칙을 느슨하게 푸는 것보다 이 표를 늘리는 쪽이 안전하다 —
#: 규칙을 풀면 어디까지 번지는지 알 수 없지만, 이 표는 적은 것만 맞춘다.
_ALIAS_PAIRS: tuple[tuple[frozenset[str], frozenset[str]], ...] = (
    # Metal DAO 의 L2 — 국내는 "Metal L2", 바이낸스는 "Metal DAO L2"
    (frozenset({"metal", "l2"}), frozenset({"metal", "dao", "l2"})),
)


def _alias_match(a: frozenset[str], b: frozenset[str]) -> bool:
    return any((a == x and b == y) or (a == y and b == x) for x, y in _ALIAS_PAIRS)


class Verdict(str, Enum):
    """국내 네트워크를 해외에서 찾은 결과."""

    MATCHED = "matched"
    ABSENT = "absent"
    UNKNOWN = "unknown"


def tokens(name: str) -> frozenset[str]:
    """네트워크 이름을 비교 가능한 토큰 집합으로 만든다.

    괄호 주석("(ERC20)")을 떼고, 기호를 지우고, 장식어를 걸러낸 뒤 별칭을
    적용한다. 집합이라 단어 순서를 타지 않는다 — "Polkadot Asset Hub" 와
    "Asset Hub Polkadot" 이 같아지는 지점이다.
    """
    cleaned = re.sub(r"\(.*?\)", " ", (name or "").lower())
    raw = [t for t in re.split(r"[^a-z0-9]+", cleaned) if t]
    return frozenset(
        _TOKEN_ALIAS.get(t, t) for t in raw if t and t not in _STOPWORDS
    )


def _overlaps(a: frozenset[str], b: frozenset[str]) -> bool:
    """토큰이 조금이라도 겹치나 (접두사 관계 포함).

    겹치면 "다른 체인이라 단정할 수 없다"는 뜻이다. 접두사까지 겹침으로
    보는 이유는 "KAT" 과 "Katana" 같은 축약을 다른 체인으로 잘라내지 않기
    위해서다 — 확실하지 않으면 UNKNOWN 으로 남긴다.
    """
    if a & b:
        return True
    return any(
        len(x) >= 3 and len(y) >= 3 and (x.startswith(y) or y.startswith(x))
        for x in a
        for y in b
    )


def find(
    domestic: NetworkStatus, foreign: tuple[NetworkStatus, ...]
) -> tuple[Verdict, NetworkStatus | None]:
    """국내 네트워크에 대응하는 해외 네트워크를 찾는다."""
    if not foreign:
        # 해외 쪽 네트워크 정보 자체가 없다 — 없는 것과 다르다
        return Verdict.UNKNOWN, None

    dom_code = (domestic.code or "").upper()
    for net in foreign:
        if dom_code and dom_code == (net.code or "").upper():
            return Verdict.MATCHED, net

    dom_tokens = tokens(domestic.name)
    if dom_tokens:
        for net in foreign:
            if dom_tokens == tokens(net.name):
                return Verdict.MATCHED, net

        # 토큰 경계만 다른 같은 이름 — 붙여 쓴 것과 띄어 쓴 것.
        # ("AssetHub Polkadot" ↔ "Asset Hub Polkadot")
        dom_flat = "".join(sorted(dom_tokens))
        for net in foreign:
            if dom_flat and dom_flat == "".join(sorted(tokens(net.name))):
                return Verdict.MATCHED, net

        for net in foreign:
            if _alias_match(dom_tokens, tokens(net.name)):
                return Verdict.MATCHED, net

    # 못 찾았다. 다른 체인인지, 이름을 못 맞춘 것인지 가른다.
    if any(_overlaps(dom_tokens, tokens(net.name)) for net in foreign):
        return Verdict.UNKNOWN, None
    return Verdict.ABSENT, None


def choose(
    domestic: tuple[NetworkStatus, ...], foreign: tuple[NetworkStatus, ...]
) -> tuple[NetworkStatus | None, Verdict, NetworkStatus | None]:
    """국내 네트워크 하나를 골라 해외 대응 망까지 찾는다.

    국내가 여러 망을 지원하는 코인은 양쪽 합쳐 열몇 개뿐이다 (USDT·USDC·
    POL·WLD 등). **실제로 옮길 수 있는 망을 우선** 고른다 — 옮길 수 있는
    길이 하나라도 있으면 그 길을 보여주는 게 맞다.

    Returns:
        (고른 국내 망, 판정, 대응 해외 망)
    """
    if not domestic:
        return None, Verdict.UNKNOWN, None

    best: tuple[NetworkStatus, Verdict, NetworkStatus | None] | None = None
    for dom in domestic:
        verdict, fx = find(dom, foreign)
        # 양쪽이 다 열린 길을 찾으면 더 볼 것 없다
        if verdict is Verdict.MATCHED and fx is not None:
            if dom.deposit and fx.withdrawal:
                return dom, verdict, fx
            if best is None or best[1] is not Verdict.MATCHED:
                best = (dom, verdict, fx)
        elif best is None:
            best = (dom, verdict, fx)

    return best if best is not None else (domestic[0], Verdict.UNKNOWN, None)


def from_json(raw: object) -> tuple[NetworkStatus, ...]:
    """저장된 네트워크 목록을 다시 NetworkStatus 로 읽는다.

    DB 컬럼이 갓 생겼을 때는 기존 행에 값이 없어 ``None`` 이 온다 — 빈
    목록으로 다룬다 (다음 수집이 채운다).
    """
    if not isinstance(raw, list):
        return ()
    return tuple(
        NetworkStatus(
            code=str(r.get("code") or ""),
            name=str(r.get("name") or r.get("code") or ""),
            deposit=bool(r.get("dep")),
            withdrawal=bool(r.get("wd")),
        )
        for r in raw
        if isinstance(r, dict)
    )
