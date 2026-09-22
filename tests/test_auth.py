"""Identity: escrowe proves who you are by opening a real connection to the
database, and keeps nothing about your password."""

import json

import pytest

from escrowe.service import AuthError, login_error_message
from escrowe.sources import Source


def test_login_opens_a_session_and_returns_who_you_are(svc):
    d = svc.login("bob", "bob")
    assert d["sub"] == "bob"
    assert svc.principal(d["token"]).user == "bob"


def test_bad_password_is_rejected(svc):
    with pytest.raises(AuthError):
        svc.login("bob", "wrong")


def test_no_password_is_retained_anywhere(svc):
    from escrowe.tokens import verify
    tok = svc.login("bob", "bob")["token"]
    session = svc.sessions[verify(svc.secret, tok)["jti"]]
    assert not hasattr(session, "password")
    assert not hasattr(session.engine, "password")


def test_logout_ends_the_session(svc):
    tok = svc.login("bob", "bob")["token"]
    assert len(svc.sessions) == 1
    svc.logout(tok)
    assert svc.sessions == {}
    svc.logout(tok)                                         # idempotent


def test_login_failure_never_leaks_driver_detail():
    """A failed login must not tell the caller whether the host, user or
    database was the problem."""
    for raw in ("Access denied for user 'alice'@'10.0.0.1'", "Unknown database 'nope'",
                "Can't connect to MySQL server on '127.0.0.1'", "FATAL: password authentication failed"):
        msg = login_error_message(Exception(raw))
        assert "alice" not in msg and "nope" not in msg and "127.0.0.1" not in msg


def test_login_against_an_unreachable_database_is_reported_as_such(svc):
    svc.source = Source("db", "mysql", {"host": "127.0.0.1", "port": 59999, "database": "nope"})
    with pytest.raises(AuthError) as e:
        svc.login("alice", "whatever")
    assert "127.0.0.1" not in str(e.value) and "whatever" not in str(e.value)


def test_persisted_config_never_contains_a_secret():
    s = Source("shop", "mysql", {"host": "h", "user": "u", "password": "sekret", "database": "d"})
    persisted = json.loads(s.persisted_json())
    assert "sekret" not in s.persisted_json()
    assert persisted == {"host": "h", "user": "u", "database": "d"}


def test_source_connected_without_persisting_leaves_nothing_on_disk(svc):
    svc.set_source(Source("other", "fake", {"host": "h", "user": "bob", "password": "bob"}), persist=False)
    assert svc.store.sources() == []
    assert svc.source.name == "other"


def test_a_saved_source_reconnects_only_once_a_password_is_supplied(tmp_path):
    from escrowe.config import Settings
    from escrowe.service import Escrowe
    from escrowe.store import Store

    store = Store(tmp_path / "saved.sqlite")
    store.save_source("fake", "fake", Source("fake", "fake", {"user": "bob", "password": "bob"}).persisted_json())
    svc = Escrowe(Settings(home=tmp_path, jwt_secret="t", llm_provider="mock"), store=store)
    assert svc.source is not None and svc.engine is None       # known, not connected
    svc.login("bob", "bob")
    assert len(svc.sessions) == 1
