"""SQL Server, end to end, against a real server. These need a live SQL
Server to connect to, so they are skipped when one isn't reachable rather
than faked with mocks - see tests/test_mysql_direct.py for the same pattern."""

import os

import pytest

pymssql = pytest.importorskip("pymssql")

HOST = os.environ.get("ESCROWE_TEST_SQLSERVER_HOST", "127.0.0.1")
PORT = int(os.environ.get("ESCROWE_TEST_SQLSERVER_PORT", "21433"))
USER = os.environ.get("ESCROWE_TEST_SQLSERVER_USER", "sa")
PASSWORD = os.environ.get("ESCROWE_TEST_SQLSERVER_PASSWORD", "Root_pw1")
DATABASE = os.environ.get("ESCROWE_TEST_SQLSERVER_DATABASE", "clientdb")


def _reachable() -> bool:
    try:
        c = pymssql.connect(server=HOST, port=PORT, user=USER, password=PASSWORD,
                            database=DATABASE, login_timeout=2)
        c.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="no live SQL Server to test the direct path against")


@pytest.fixture
def direct_svc(tmp_path):
    from escrowe.config import Settings
    from escrowe.service import Escrowe
    from escrowe.sources import Source

    settings = Settings(home=tmp_path, llm_provider="mock")
    svc = Escrowe(settings)
    svc.set_source(Source("clientdb", "sqlserver", {
        "host": HOST, "port": PORT, "user": USER, "password": PASSWORD,
        "database": DATABASE}), persist=False)
    return svc


def test_direct_engine_bypasses_duckdb_entirely(direct_svc):
    from escrowe.engines.sqlserver import SQLServerEngine
    assert isinstance(direct_svc.engine, SQLServerEngine)


def test_direct_login_and_query_use_the_real_sqlserver_grant(direct_svc):
    d = direct_svc.login(USER, PASSWORD)
    p = direct_svc.principal(d["token"])
    res = direct_svc.sql(p, "SELECT * FROM customers")
    assert res.table.num_rows >= 0
    assert "id" in res.table.column_names


def test_direct_login_rejects_a_bad_password(direct_svc):
    from escrowe.service import AuthError
    with pytest.raises(AuthError):
        direct_svc.login(USER, "definitely-wrong")


def test_direct_mode_still_blocks_writes_for_the_agent(direct_svc):
    from escrowe.guard import Denied
    d = direct_svc.login(USER, PASSWORD)
    p = direct_svc.principal(d["token"])
    with pytest.raises(Denied):
        direct_svc.sql(p, "DELETE FROM orders", allow_write=False)


def test_direct_catalog_reads_real_sqlserver_tables(direct_svc):
    from escrowe.metadata import read_schema, render_schema
    text = render_schema(read_schema(direct_svc.engine))
    assert "customers" in text
