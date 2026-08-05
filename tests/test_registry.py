"""레지스트리 자동 등록 테스트."""

from __future__ import annotations

import inspect

import pytest

from app.core.errors import UnsupportedExchangeError
from app.exchanges import all_exchanges, exchange_ids, get_exchange
from app.exchanges.base import BaseExchange
from app.exchanges.registry import _discover, _is_connector, _iter_connector_modules


class TestAutoDiscovery:
    def test_finds_connectors_without_manual_registration(self) -> None:
        # registry.py 에 거래소 이름이 하드코딩되어 있지 않아도 발견되어야 한다.
        assert set(exchange_ids()) >= {"upbit", "binance"}

    def test_every_connector_module_is_imported(self) -> None:
        modules = _iter_connector_modules()
        assert {m.__name__.rsplit(".", 1)[-1] for m in modules} >= {"upbit", "binance"}

    def test_discovered_classes_are_concrete_subclasses(self) -> None:
        for exchange in all_exchanges():
            assert isinstance(exchange, BaseExchange)
            assert not inspect.isabstract(type(exchange))

    def test_base_class_itself_is_not_registered(self) -> None:
        assert BaseExchange not in {type(e) for e in all_exchanges()}

    def test_abstract_subclass_is_skipped(self) -> None:
        """추상 메서드가 남은 미완성 클래스는 등록 대상이 아니다."""

        class Incomplete(BaseExchange):
            id = "incomplete"
            name = "미완성"
            quote_currencies = frozenset({"KRW"})
            default_quote = "KRW"
            # 추상 메서드를 구현하지 않음

        module = inspect.getmodule(TestAutoDiscovery)
        assert _is_connector(Incomplete, module) is False

    def test_registry_is_not_empty(self) -> None:
        assert _discover()


class TestLookup:
    @pytest.mark.parametrize("exchange_id", ["upbit", "UPBIT", " binance "])
    def test_lookup_is_case_and_space_insensitive(self, exchange_id: str) -> None:
        assert get_exchange(exchange_id) is not None

    def test_returns_singleton(self) -> None:
        assert get_exchange("upbit") is get_exchange("upbit")

    def test_unknown_id_lists_supported_exchanges(self) -> None:
        with pytest.raises(UnsupportedExchangeError) as exc_info:
            get_exchange("coinbase")

        assert exc_info.value.detail["supported"] == exchange_ids()

    def test_ids_match_class_attribute(self) -> None:
        for exchange in all_exchanges():
            assert get_exchange(exchange.id) is exchange
