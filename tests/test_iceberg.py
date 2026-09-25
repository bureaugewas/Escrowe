"""Iceberg REST catalogs. Parsing and secret handling run everywhere; the
end-to-end tests need a live catalog with at least one table, named by:

    ESCROWE_TEST_ICEBERG_ENDPOINT    e.g. http://localhost:8181/api/catalog
    ESCROWE_TEST_ICEBERG_WAREHOUSE   the catalog name
    ESCROWE_TEST_ICEBERG_TOKEN       a bearer token, or instead:
    ESCROWE_TEST_ICEBERG_CLIENT_ID / ESCROWE_TEST_ICEBERG_CLIENT_SECRET
"""

from __future__ import annotations

import json
import os

import pytest

from escrowe.engines import EngineError
from escrowe.sources import Source, parse_dsn


def test_dsn_with_a_token():
    src = parse_dsn("iceberg:http://polaris:8181/api/catalog?warehouse=db-x&token=T0K")
    assert (src.name, src.kind) == ("lake", "iceberg")
    assert src.params == {"endpoint": "http://polaris:8181/api/catalog", "warehouse": "db-x", "token": "T0K"}
    assert src.secret_key == "token"


def test_dsn_with_client_credentials():
    src = parse_dsn("sales=iceberg:https://h/api/catalog?warehouse=w&client_id=c&client_secret=s"
                    "&oauth2_server_uri=https://idp/token&scope=PRINCIPAL_ROLE:ALL")
    assert src.name == "sales"
    assert src.params["client_id"] == "c" and src.params["scope"] == "PRINCIPAL_ROLE:ALL"
    assert src.secret_key == "client_secret"


def test_dsn_needs_a_warehouse():
    with pytest.raises(ValueError, match="warehouse"):
        parse_dsn("iceberg:http://polaris:8181/api/catalog?token=T0K")


def test_neither_token_nor_client_secret_reaches_disk():
    src = Source("lake", "iceberg", {"endpoint": "e", "warehouse": "w", "token": "T0K",
                                     "client_id": "c", "client_secret": "S3CRET"})
    persisted = json.loads(src.persisted_json())
    assert "T0K" not in str(persisted) and "S3CRET" not in str(persisted)
    assert persisted == {"endpoint": "e", "warehouse": "w", "client_id": "c"}


def test_no_credentials_is_refused_before_any_network_call():
    from escrowe.engines.iceberg import IcebergEngine
    with pytest.raises(EngineError, match="token"):
        IcebergEngine(endpoint="http://127.0.0.1:1/api/catalog", warehouse="w")


# ------------------------------------------------------------------ live

_ENV = {k: os.environ.get(f"ESCROWE_TEST_ICEBERG_{k.upper()}")
        for k in ("endpoint", "warehouse", "token", "client_id", "client_secret")}


@pytest.fixture
def lake_svc(tmp_path):
    if not (_ENV["endpoint"] and _ENV["warehouse"] and (_ENV["token"] or _ENV["client_secret"])):
        pytest.skip("no live Iceberg catalog configured (ESCROWE_TEST_ICEBERG_*)")
    from escrowe.config import Settings
    from escrowe.service import Escrowe
    svc = Escrowe(Settings(home=tmp_path, llm_provider="mock"))
    svc.set_source(Source("lake", "iceberg", {k: v for k, v in _ENV.items() if v}), persist=False)
    return svc


def test_catalog_lists_the_tables(lake_svc):
    from escrowe.metadata import read_schema
    assert read_schema(lake_svc.engine)


def test_a_query_runs_against_the_real_data(lake_svc):
    from escrowe.metadata import read_schema
    first = read_schema(lake_svc.engine)[0].fqn
    res = lake_svc.sql(lake_svc.operator_principal(), f"SELECT count(*) AS n FROM {first}")
    assert res.table.num_rows == 1


def test_write_still_blocked_for_the_agent(lake_svc):
    from escrowe.guard import Denied
    from escrowe.metadata import read_schema
    first = read_schema(lake_svc.engine)[0].fqn
    with pytest.raises(Denied, match="only read"):
        lake_svc.sql(lake_svc.operator_principal(), f"DELETE FROM {first}", allow_write=False)
