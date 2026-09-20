"""Session/identity edges (revoked or forged sessions must not fall back to a
more-privileged engine), the local-operator loopback bypass (must not trust
spoofable headers), resource limits (query_timeout_s, max_rows), and guard.py
edges beyond test_guard.py's hard denials: stacked statements, comments
hiding a second statement, case variation, DDL, CALL/EXEC/SELECT...INTO, and
a unicode/homoglyph trick."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from escrowe.guard import Denied, check
from escrowe.server import create_app
from escrowe.service import AuthError, Principal


# ------------------------------------------------------------- session/auth
def test_engine_for_raises_for_a_revoked_session_not_silent_fallback(svc):
    token = svc.login("bob", "bob")["token"]
    p = svc.principal(token)
    svc.logout(token)
    with pytest.raises(AuthError):
        svc.engine_for(p)


def test_engine_for_raises_for_an_expired_session(svc):
    token = svc.login("bob", "bob")["token"]
    p = svc.principal(token)
    svc.sessions[p.session].created = 0    # force it past token_ttl_s
    svc._reap_sessions()
    with pytest.raises(AuthError):
        svc.engine_for(p)


def test_engine_for_raises_for_a_forged_session_id_that_never_existed(svc):
    p = Principal(user="bob", session="forged-session-id-never-issued")
    with pytest.raises(AuthError):
        svc.engine_for(p)


def test_a_session_principal_never_silently_becomes_the_operator_engine(svc):
    """A principal that carries *some* session id, even a bad one, must never
    fall through to self.engine (the operator's own, possibly more
    privileged connection) - only session=None (the CLI's own operator mode)
    may use it."""
    forged = Principal(user="bob", session="not-a-real-session")
    with pytest.raises(AuthError):
        svc.engine_for(forged)
    operator = Principal(user="whoever", session=None)
    assert svc.engine_for(operator) is svc.engine


# ------------------------------------------------------------- loopback bypass
def test_spoofed_forwarded_for_header_does_not_grant_local_operator_bypass(svc):
    """is_local() in server.py must key off the actual peer address, not a
    client-supplied header - otherwise any remote caller could claim to be
    127.0.0.1 and skip authentication entirely."""
    client = TestClient(create_app(svc, local_operator=True))
    r = client.get("/me", headers={"X-Forwarded-For": "127.0.0.1", "Host": "127.0.0.1",
                                   "X-Real-IP": "127.0.0.1"})
    assert r.status_code == 401


def test_local_operator_bypass_works_for_an_actually_local_peer(svc):
    """Sanity check for the test above: the bypass exists and does work when
    the real peer address (not a header) is loopback, so the 401 above is
    proof the header is ignored, not proof the feature is broken."""
    client = TestClient(create_app(svc, local_operator=True), client=("127.0.0.1", 12345))
    r = client.get("/me")
    assert r.status_code == 200
    assert r.json()["operator"] is True

    # the same spoofed headers, now from a genuinely remote peer, still don't help
    remote = TestClient(create_app(svc, local_operator=True), client=("203.0.113.5", 12345))
    r2 = remote.get("/me", headers={"X-Forwarded-For": "127.0.0.1"})
    assert r2.status_code == 401


# ------------------------------------------------------------------ limits
def test_max_rows_caps_what_a_query_returns(svc, alice):
    svc.settings.max_rows = 2
    res = svc.sql(alice, "SELECT * FROM orders")
    assert res.table.num_rows <= 2


def test_query_timeout_setting_is_passed_through_to_the_engine(svc, alice, monkeypatch):
    seen = {}
    original = type(svc.engine).execute

    def spy(self, sql, timeout_s=None):
        seen["timeout_s"] = timeout_s
        return original(self, sql, timeout_s)

    monkeypatch.setattr(type(svc.engine), "execute", spy)
    svc.settings.query_timeout_s = 12.5
    svc.sql(alice, "SELECT count(*) AS n FROM customers")
    assert seen["timeout_s"] == 12.5


# -------------------------------------------------------------- guard edges
def test_stacked_statements_via_semicolon_are_denied():
    with pytest.raises(Denied, match="one statement"):
        check("SELECT 1; DROP TABLE customers;", allow_write=True, dialect="mysql")


def test_stacked_statements_are_denied_even_when_the_second_is_a_read():
    with pytest.raises(Denied, match="one statement"):
        check("SELECT 1; SELECT 2;", allow_write=True, dialect="mysql")


def test_a_comment_cannot_smuggle_a_second_statement_past_the_count():
    # the DROP is actually commented out here, so this is genuinely one
    # statement and must be treated as a harmless (if odd) read - proving
    # the counter looks at real statements, not just semicolon-splitting.
    sql = "SELECT 1 /* ; DROP TABLE customers; */"
    assert check(sql, allow_write=False, dialect="mysql")


def test_case_variation_does_not_evade_the_write_check():
    for variant in ("insert into t values (1)", "InSeRt into t values (1)", "INSERT into t values (1)"):
        with pytest.raises(Denied, match="only read"):
            check(variant, allow_write=False, dialect="mysql")


def test_a_leading_comment_does_not_evade_the_write_check():
    with pytest.raises(Denied, match="only read"):
        check("/*comment*/INSERT INTO t VALUES(1)", allow_write=False, dialect="mysql")


@pytest.mark.parametrize("sql", [
    "DROP TABLE customers",
    "ALTER TABLE customers ADD COLUMN x INT",
    "TRUNCATE TABLE customers",
])
def test_ddl_is_denied_for_the_agent_but_allowed_for_a_person(sql):
    with pytest.raises(Denied, match="only read"):
        check(sql, allow_write=False, dialect="mysql")
    assert check(sql, allow_write=True, dialect="mysql")


def test_exec_is_denied_regardless_of_allow_write():
    for allow_write in (False, True):
        with pytest.raises(Denied):
            check("EXEC my_proc 1, 2", allow_write=allow_write, dialect="tsql")


def test_select_into_creates_a_table_and_is_denied_for_the_agent():
    """SELECT ... INTO (SQL Server/Sybase) parses as an ordinary SELECT but
    creates a table - a write wearing a read's clothes, exactly like INTO
    OUTFILE. It must not slip past the agent's read-only restriction."""
    with pytest.raises(Denied, match="only read"):
        check("SELECT * INTO new_table FROM customers", allow_write=False, dialect="tsql")
    # a person running their own \sql may still do this deliberately
    assert check("SELECT * INTO new_table FROM customers", allow_write=True, dialect="tsql")


def test_unicode_homoglyph_keyword_does_not_evade_the_write_check():
    # a Cyrillic 'е' (U+0435) in place of the Latin 'e' in DELETE - if a
    # parser were fooled into reading this as some harmless unknown
    # identifier-based statement it would be denied anyway (not a
    # recognized read), but it must never be accepted as a read.
    sql = "DеLETE FROM customers"
    with pytest.raises(Denied):
        check(sql, allow_write=False, dialect="mysql")
