"""거래소 레지스트리 — 자동 등록(auto-discovery).

``app/exchanges/connectors/`` 안의 모든 모듈을 훑어서 ``BaseExchange`` 하위
클래스를 찾아 자동으로 등록한다. **새 거래소를 추가할 때 이 파일을 수정할 필요가 없다.**
connectors 폴더에 파일 하나만 만들면 끝이다.

동작 방식
    1. ``pkgutil.iter_modules`` 로 connectors 패키지 안의 모듈 이름을 나열
    2. ``importlib.import_module`` 로 각 모듈을 임포트
    3. ``inspect.getmembers`` 로 모듈 안의 클래스를 훑어 조건에 맞는 것만 등록

등록 조건
    - ``BaseExchange`` 를 상속했고 ``BaseExchange`` 자신은 아님
    - 추상 메서드가 전부 구현됨 (미완성 중간 클래스는 건너뜀)
    - **그 모듈에서 정의된** 클래스 (다른 모듈에서 import 해온 것은 제외)
    - ``id`` 클래스 속성을 가짐
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from types import ModuleType

from app.core.errors import UnsupportedExchangeError
from app.exchanges import connectors
from app.exchanges.base import BaseExchange


def _iter_connector_modules() -> list[ModuleType]:
    """connectors 패키지 안의 모든 모듈을 임포트해서 반환한다."""
    modules = []
    for info in pkgutil.iter_modules(connectors.__path__, prefix=f"{connectors.__name__}."):
        if info.name.rsplit(".", 1)[-1].startswith("_"):
            continue  # _private.py 같은 내부 모듈은 건너뛴다
        modules.append(importlib.import_module(info.name))
    return modules


def _is_connector(obj: object, module: ModuleType) -> bool:
    """등록 대상 거래소 클래스인지 판정한다."""
    return (
        inspect.isclass(obj)
        and issubclass(obj, BaseExchange)
        and obj is not BaseExchange
        and not inspect.isabstract(obj)          # 추상 메서드가 남아있으면 제외
        and obj.__module__ == module.__name__    # import 해온 클래스는 제외
        and isinstance(getattr(obj, "id", None), str)
    )


def _discover() -> dict[str, BaseExchange]:
    """connectors 패키지를 훑어 {거래소 ID: 인스턴스} 사전을 만든다.

    커넥터는 상태를 갖지 않으므로 프로세스당 하나만 만들어 재사용한다.

    Raises:
        RuntimeError: 서로 다른 두 클래스가 같은 ``id`` 를 선언한 경우.
    """
    found: dict[str, BaseExchange] = {}
    origins: dict[str, str] = {}

    for module in _iter_connector_modules():
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if not _is_connector(cls, module):
                continue

            if cls.id in origins:
                raise RuntimeError(
                    f"거래소 ID 가 중복되었습니다: {cls.id!r} "
                    f"({origins[cls.id]} 와 {cls.__module__}.{cls.__name__})"
                )

            origins[cls.id] = f"{cls.__module__}.{cls.__name__}"
            found[cls.id] = cls()

    return found


#: 최초 import 시 한 번만 실행된다.
_REGISTRY: dict[str, BaseExchange] = _discover()


def get_exchange(exchange_id: str) -> BaseExchange:
    """거래소 ID 로 커넥터를 가져온다.

    Raises:
        UnsupportedExchangeError: 등록되지 않은 ID.
    """
    try:
        return _REGISTRY[exchange_id.strip().lower()]
    except KeyError as exc:
        raise UnsupportedExchangeError(
            f"지원하지 않는 거래소입니다: {exchange_id!r}. "
            f"지원 거래소: {', '.join(exchange_ids())}",
            detail={"supported": exchange_ids()},
        ) from exc


def all_exchanges() -> list[BaseExchange]:
    """등록된 모든 커넥터를 반환한다."""
    return list(_REGISTRY.values())


def exchange_ids() -> list[str]:
    """등록된 모든 거래소 ID 를 반환한다."""
    return sorted(_REGISTRY)


def reload() -> list[str]:
    """레지스트리를 다시 스캔한다 (개발 중 커넥터를 추가했을 때 사용).

    Returns:
        재스캔 후 등록된 거래소 ID 목록.
    """
    global _REGISTRY
    _REGISTRY = _discover()
    return exchange_ids()
