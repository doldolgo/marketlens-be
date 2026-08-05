"""거래소 커넥터 패키지.

개별 거래소 구현은 ``connectors/`` 안에 있고, 레지스트리가 자동으로 찾아 등록한다.
바깥 코드는 구체 클래스를 직접 import 하지 말고 ``get_exchange(id)`` 로 가져온다.
"""

from app.exchanges.base import BaseExchange
from app.exchanges.registry import all_exchanges, exchange_ids, get_exchange, reload

__all__ = [
    "BaseExchange",
    "all_exchanges",
    "exchange_ids",
    "get_exchange",
    "reload",
]
