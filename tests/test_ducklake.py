"""DuckLake, end to end, against a real local catalog. Needs the `ducklake`
DuckDB extension to be downloadable (it isn't in every sandbox - extension
downloads can be blocked by network policy), so these are skipped rather
than faked when it can't load."""

import shutil

import pytest

duckdb = pytest.importorskip("duckdb")


def _ducklake_available() -> bool:
    try:
        c = duckdb.connect()
        c.execute("INSTALL ducklake; LOAD ducklake;")
        c.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _ducklake_available(),
                                reason="the ducklake DuckDB extension could not be loaded here")


@pytest.fixture
def lake_svc(tmp_path):
    from escrowe.config import load_settings
    from escrowe.service import Escrowe
    from escrowe.sources import Source

    # Build a small real DuckLake so the test proves something, not just
    # that ATTACH doesn't crash.
    catalog = tmp_path / "catalog.duckdb"
    data = tmp_path / "data"
    data.mkdir()
    con = duckdb.connect()
    con.execute("INSTALL ducklake; LOAD ducklake;")
    con.execute(f"ATTACH 'ducklake:{catalog}' AS lake (DATA_PATH '{data}/')")
    con.execute("CREATE TABLE lake.customers AS SELECT 1 AS id, 'Acme' AS name")
    con.execute("COMMENT ON TABLE lake.customers IS 'Customer master data'")
    con.close()

    settings = load_settings(ephemeral=True)
    settings.home = tmp_path
    svc = Escrowe(settings)
    svc.set_source(Source("lake", "ducklake", {"metadata": str(catalog)}), persist=False)
    return svc


def test_ducklake_connects_with_no_login_step(lake_svc):
    from escrowe.engines.ducklake import DuckLakeEngine
    assert isinstance(lake_svc.engine, DuckLakeEngine)
    assert lake_svc.engine.requires_credentials is False


def test_ducklake_catalog_reads_the_real_catalog(lake_svc):
    from escrowe.metadata import Metadata
    text = Metadata.render(Metadata(lake_svc.engine).all_tables())
    assert "customers" in text and "Customer master data" in text


def test_ducklake_query_runs_against_the_real_data(lake_svc):
    p = lake_svc.operator_principal()
    res = lake_svc.sql(p, "SELECT * FROM customers")
    assert res.table.to_pylist() == [{"id": 1, "name": "Acme"}]


def test_ducklake_write_still_blocked_for_the_agent(lake_svc):
    from escrowe.guard import Denied
    p = lake_svc.operator_principal()
    with pytest.raises(Denied, match="only read"):
        lake_svc.sql(p, "DELETE FROM customers", allow_write=False)
