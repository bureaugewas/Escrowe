"""Nothing that identifies how to connect to the real database - passwords
above all - should ever be persisted to disk, returned over the API, put in
an error message, embedded in a token, or written to the audit log."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from escrowe.engines import KINDS, EngineError, build
from escrowe.server import create_app
from escrowe.service import AuthError
from escrowe.sources import SECRET_KEYS, Source
from escrowe.tokens import TokenError, issue, verify


# --------------------------------------------------------------- sources.py
@pytest.mark.parametrize("kind", KINDS)
def test_persisted_json_never_contains_a_password_for_any_engine_kind(kind):
    src = Source("s", kind, {"password": "sekret999", "user": "u", "host": "h", "metadata": "m"})
    persisted = src.persisted_json()
    assert "sekret999" not in persisted
    assert "password" not in json.loads(persisted)


def test_persisted_json_never_contains_a_ducklake_quack_token():
    """A Quack token authenticates a hosted DuckLake catalog exactly like a
    password authenticates an account - it must never reach disk either."""
    src = Source("lake", "ducklake", {"metadata": "quack:host:443", "token": "TOK-secret", "data_path": "s3://x"})
    persisted = src.persisted_json()
    assert "TOK-secret" not in persisted
    assert "token" not in json.loads(persisted)


def test_redacted_masks_every_secret_key():
    params = {k: "raw-secret-value" for k in SECRET_KEYS}
    params["host"] = "h"
    src = Source("s", "mysql", params)
    red = src.redacted()["params"]
    for k in SECRET_KEYS:
        assert red[k] != "raw-secret-value"
    assert red["host"] == "h"


def test_bad_password_error_never_contains_the_attempted_password():
    with pytest.raises(EngineError) as e:
        build("fake", user="alice", password="TOTALLY_WRONG_PW_123")
    assert "TOTALLY_WRONG_PW_123" not in str(e.value)


def test_login_failure_message_never_contains_the_password(svc):
    with pytest.raises(AuthError) as e:
        svc.login("alice", "WRONG_PASSWORD_ABC")
    assert "WRONG_PASSWORD_ABC" not in str(e.value)


# ---------------------------------------------------------------- server.py
def test_server_health_never_exposes_a_raw_password(svc):
    svc._source = Source("fake", "fake", {"user": "alice", "password": "SUPERSECRETPW99", "host": "h"})
    client = TestClient(create_app(svc))
    r = client.get("/health")
    assert r.status_code == 200
    assert "SUPERSECRETPW99" not in r.text


def test_server_sources_never_exposes_a_raw_password(svc):
    token = svc.login("bob", "bob")["token"]
    svc._source = Source("fake", "fake", {"user": "alice", "password": "SUPERSECRETPW99", "host": "h"})
    client = TestClient(create_app(svc))
    r = client.get("/sources", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert "SUPERSECRETPW99" not in r.text


# ---------------------------------------------------------------- tokens.py
def test_login_issues_a_token_with_only_expected_claims(svc):
    token = svc.login("bob", "bob")["token"]
    claims = verify(svc.secret, token)
    assert set(claims) == {"sub", "iat", "exp", "jti"}
    assert svc.secret not in token


def test_issued_token_never_leaks_the_signing_secret():
    secret = "MY_TOP_SECRET_JWT_SIGNING_KEY"
    token = issue(secret, {"sub": "alice"}, 3600)
    assert secret not in token


def test_tampered_signature_fails_verification(svc):
    """Flips a character in the middle of the signature, not the last one: an
    unpadded base64 string's final character can carry unused "don't care"
    bits (32 signature bytes % 3 leaves a partial group), so a flip there can
    coincidentally decode to the same bytes and make this test flaky."""
    token = svc.login("bob", "bob")["token"]
    h, p, s = token.split(".")
    mid = len(s) // 2
    flipped = s[:mid] + ("A" if s[mid] != "A" else "B") + s[mid + 1:]
    with pytest.raises(TokenError):
        verify(svc.secret, f"{h}.{p}.{flipped}")


def test_tampered_payload_fails_verification(svc):
    token = svc.login("bob", "bob")["token"]
    h, p, s = token.split(".")
    flipped = p[:-1] + ("A" if p[-1:] != "A" else "B")
    with pytest.raises(TokenError):
        verify(svc.secret, f"{h}.{flipped}.{s}")


# ----------------------------------------------------------------- store.py
def test_audit_preserves_password_like_literals_unmangled(svc, alice):
    """escrowe does not rewrite SQL - a password-looking string literal a
    person typed themselves is stored exactly as written, not masked."""
    svc.sql(alice, "SELECT 'not-a-real-password' AS x")
    row = svc.store.audit_rows(alice.user)[0]
    assert row["candidate_sql"] == "SELECT 'not-a-real-password' AS x"
    assert row["compiled_sql"] == "SELECT 'not-a-real-password' AS x"


def test_audit_log_never_contains_the_actual_connection_password(svc, alice):
    """The account's own login password (here "alice") never flows through
    the query path at all, so it can never end up in an audit row."""
    svc.sql(alice, "SELECT count(*) AS n FROM customers")
    rows = svc.store.audit_rows(alice.user)
    for row in rows:
        for col in ("candidate_sql", "compiled_sql", "reason"):
            assert row[col] is None or "password" not in row[col].lower()
