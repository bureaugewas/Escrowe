"""SQLite, end to end, against a real file - no container needed. Run
docker/seed_sqlite.py first to produce the sample file this test reads,
mirroring tests/test_mysql_direct.py's real-engine pattern (no mocks)."""

import os
import shutil
import sqlite3
from pathlib import Path

import pytest

SAMPLE = Path(os.environ.get("ESCROWE_TEST_SQLITE_PATH",
                              Path(__file__).parent.parent / "docker" / "sample.sqlite"))

pytestmark = pytest.mark.skipif(not SAMPLE.exists(),
                                reason="run `python docker/seed_sqlite.py` first")


@pytest.fixture
def db_path(tmp_path):
    # Copy so the write-blocking test can't corrupt the shared sample file.
    p = tmp_path / "sample.sqlite"
    shutil.copyfile(SAMPLE, p)
    return str(p)


@pytest.fixture
def direct_svc(tmp_path, db_path):
    from escrowe.config import load_settings
    from escrowe.service import Escrowe
    from escrowe.sources import Source

    settings = load_settings(ephemeral=True)
    settings.home = tmp_path
    svc = Escrowe(settings)
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
    from escrowe.metadata import Metadata
    text = Metadata.render(Metadata(direct_svc.engine).all_tables())
    assert "customers" in text
    assert "orders" in text
