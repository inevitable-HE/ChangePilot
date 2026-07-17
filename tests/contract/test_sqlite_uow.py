from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from tests.contract.uow_contract import UnitOfWorkContract, _load_module


ROOT = Path(__file__).resolve().parents[2]


def _database_url(path: Path) -> str:
    return f"sqlite:///{path.resolve().as_posix()}"


def _alembic_config(database_path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", _database_url(database_path))
    return config


def _upgrade_to_head(database_path: Path) -> None:
    command.upgrade(_alembic_config(database_path), "head")


@pytest.fixture
def adapter_modules():
    return {
        "sqlite": _load_module("changepilot.workflow.adapters.persistence.sqlite"),
    }


class TestSQLiteUnitOfWork(UnitOfWorkContract):
    __test__ = True

    @pytest.fixture
    def store(self, tmp_path: Path, adapter_modules):
        database_path = tmp_path / "workflow-runtime.db"
        _upgrade_to_head(database_path)
        engine = adapter_modules["sqlite"].create_sqlite_engine(database_path)
        try:
            yield engine
        finally:
            engine.dispose()

    def make_uow(self, adapter_modules, store):
        return adapter_modules["sqlite"].SQLiteUnitOfWork(store)
