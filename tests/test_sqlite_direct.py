"""SQLite, end to end, against a real file. No container is needed: the
sample database is generated into a temp directory by tests/docker/seed_sqlite.py."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "docker"))
from seed_sqlite import seed  # noqa: E402


@pytest.fixture
def db_path(tmp_path):
    return str(seed(tmp_path / "sample.sqlite"))


@pytest.fixture
def direct_svc(tmp_path, db_path):
    from escrowe.config import Settings
    from escrowe.service import Escrowe
    from escrowe.sources import Source

    svc = Escrowe(Settings(home=tmp_path, llm_provider="mock"))
    svc.set_source(Source("clientdb", "sqlite", {"path": db_path}), persist=False)
    return svc


def test_direct_engine_bypasses_duckdb_entirely(direct_svc):
    from escrowe.engines.sqlite import SQLiteEngine
    assert isinstance(direct_svc.engine, SQLiteEngine)


def test_no_login_step_needed(direct_svc):
    assert direct_svc.engine.requires_credentials is False


def test_direct_query_returns_real_data(direct_svc):
    p = direct_svc.operator_principal()
    res = direct_svc.sql(p, "SELECT * FROM customers LIMIT 5")
    assert res.table.num_rows == 5
    assert "id" in res.table.column_names


def test_direct_mode_still_blocks_writes_for_the_agent(direct_svc):
    from escrowe.guard import Denied
    p = direct_svc.operator_principal()
    with pytest.raises(Denied):
        direct_svc.sql(p, "DELETE FROM orders", allow_write=False)


def test_direct_catalog_reads_real_sqlite_tables(direct_svc):
    from escrowe.metadata import read_schema, render_schema
    text = render_schema(read_schema(direct_svc.engine))
    assert "customers" in text
    assert "orders" in text
