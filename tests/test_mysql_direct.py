"""MySQL, end to end, against a real server. These need a live MySQL/MariaDB
to connect to, so they are skipped when one isn't reachable rather than faked
with mocks - the whole point is that escrowe connects with a real driver,
not a rewritten query running through some other engine in between."""

import os

import pytest

pymysql = pytest.importorskip("pymysql")

HOST = os.environ.get("ESCROWE_TEST_MYSQL_HOST", "127.0.0.1")
PORT = int(os.environ.get("ESCROWE_TEST_MYSQL_PORT", "3306"))
USER = os.environ.get("ESCROWE_TEST_MYSQL_USER", "pilot_ro")
PASSWORD = os.environ.get("ESCROWE_TEST_MYSQL_PASSWORD", "pilot-dev-pw")
DATABASE = os.environ.get("ESCROWE_TEST_MYSQL_DATABASE", "clientdb")


def _reachable() -> bool:
    try:
        c = pymysql.connect(host=HOST, port=PORT, user=USER, password=PASSWORD,
                            database=DATABASE, connect_timeout=2)
        c.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="no live MySQL to test the direct path against")


@pytest.fixture
def direct_svc(tmp_path):
    from escrowe.config import load_settings
    from escrowe.service import Escrowe
    from escrowe.sources import Source

    settings = load_settings(ephemeral=True)
    settings.home = tmp_path
    svc = Escrowe(settings)
    svc.set_source(Source("clientdb", "mysql", {
        "host": HOST, "port": PORT, "user": USER, "password": PASSWORD,
        "database": DATABASE}), persist=False)
    return svc


def test_direct_engine_bypasses_duckdb_entirely(direct_svc):
    from escrowe.engines.mysql import MySQLEngine
    assert isinstance(direct_svc.engine, MySQLEngine)


def test_direct_login_and_query_use_the_real_mysql_grant(direct_svc):
    d = direct_svc.login(USER, PASSWORD)
    p = direct_svc.principal(d["token"])
    res = direct_svc.sql(p, "SELECT * FROM meter_readings LIMIT 5")
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
        direct_svc.sql(p, "DELETE FROM meter_readings", allow_write=False)


def test_direct_catalog_reads_real_mysql_comments(direct_svc):
    from escrowe.metadata import Metadata
    text = Metadata.render(Metadata(direct_svc.engine).all_tables())
    assert "meter_readings" in text
