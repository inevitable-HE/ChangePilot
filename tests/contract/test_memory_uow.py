from __future__ import annotations

import pytest

from tests.contract.uow_contract import UnitOfWorkContract, _load_module


@pytest.fixture
def adapter_modules():
    return {
        "memory": _load_module("changepilot.workflow.adapters.persistence.memory"),
    }


class TestMemoryUnitOfWork(UnitOfWorkContract):
    __test__ = True
