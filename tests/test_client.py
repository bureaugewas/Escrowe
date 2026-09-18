"""The machine interface: HTTP connection (via the ASGI app) and the embedded one."""

import json

import pytest
from fastapi.testclient import TestClient

from escrowe.client import Connection, EscroweAuthError, LocalConnection, parse_dsn
from escrowe.server import create_app


@pytest.fixture
def http(svc):
    app = create_app(svc)
    return lambda user, pw: Connection("http://testserver", user, pw, client=TestClient(app))


def test_parse_dsn():
    assert parse_dsn("escrowe://alice:s%40cret@db.internal:9000") == ("http://db.internal:9000", "alice", "s@cret")
    assert parse_dsn("escrowes://h") == ("https://h:443", None, None)
    assert parse_dsn("http://127.0.0.1:8765") == ("http://127.0.0.1:8765", None, None)


def test_http_ask_and_sql_are_machine_readable(http):
    with http("bob", "bob") as conn:
        r = conn.ask("how many orders")
        d = json.loads(r.to_json())
        assert d["sql"].lower().startswith("select") and d["columns"] == ["n"] and d["rows"] == [[1370]]
        assert d["question"] == "how many orders" and d["audit_id"]
        r2 = conn.sql("SELECT count(*) AS n FROM employees")
        assert r2.records() == [{"n": 2}]
        assert r2.to_arrow().num_rows == 1
        assert "customers" in conn.metadata()["text"]


def test_http_relogin_after_expiry(http):
    conn = http("bob", "bob")
    conn.token = "expired.token.here"
    assert conn.sql("SELECT 1 AS x").rows == [[1]]     # transparently logged in again
    assert conn.token != "expired.token.here"


def test_http_bad_login(http):
    with pytest.raises(EscroweAuthError):
        http("alice", "nope")


def test_embedded_login_queries_as_that_account(svc):
    conn = LocalConnection(svc).login("bob", "bob")
    r = conn.ask("how many orders")
    assert r.rows == [[1370]] and r.sql


def test_operator_mode_queries_as_the_connected_account_without_login(svc):
    """escrowe connected as alice at startup (see conftest); the local operator
    session queries as that account with no separate login step."""
    op = LocalConnection(svc, operator=True)
    assert op.ask("how many orders").rows == [[1370]]
    assert op.sql("SELECT count(*) AS n FROM customers").rows == [[3]]
    assert [s["name"] for s in op.sources()["sources"]] == ["fake"]


def test_a_plain_connection_with_no_login_and_no_operator_flag_cannot_query(svc):
    conn = LocalConnection(svc)
    with pytest.raises(EscroweAuthError):
        conn.ask("anything")
