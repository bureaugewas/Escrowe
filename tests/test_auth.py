"""Identity: escrowe proves who you are by opening a real connection to the
database, and keeps nothing about your password."""

import pytest

from escrowe.auth import AuthError as VerifyError, DirectVerifier, build_verifier
from escrowe.service import AuthError
from escrowe.sources import Source


def test_login_opens_a_session_and_returns_who_you_are(svc):
    d = svc.login("bob", "bob")
    assert d["sub"] == "bob"
    assert svc.principal(d["token"]).user == "bob"


def test_bad_password_is_rejected(svc):
    with pytest.raises(AuthError):
        svc.login("bob", "wrong")


def test_no_password_is_retained_anywhere(svc):
    tok = svc.login("bob", "bob")["token"]
    from escrowe.tokens import verify
    session = svc.sessions[verify(svc.secret, tok)["jti"]]
    assert not hasattr(session, "password")
    assert not hasattr(session.engine, "password")


def test_logout_ends_the_session(svc):
    tok = svc.login("bob", "bob")["token"]
    assert len(svc.sessions) == 1
    svc.logout(tok)
    assert svc.sessions == {}
    svc.logout(tok)                                         # idempotent


def test_direct_verifier_opens_a_real_connection(svc):
    v = build_verifier(svc.source())
    assert isinstance(v, DirectVerifier)
    assert v.verify("bob", "bob").user == "bob"
    with pytest.raises(VerifyError):
        v.verify("bob", "nope")


def test_verifier_never_leaks_driver_detail():
    """A failed login must not tell the caller whether the host, user or
    database was the problem."""
    v = DirectVerifier("mysql", {"host": "127.0.0.1", "port": 59999, "database": "nope"})
    with pytest.raises(VerifyError) as e:
        v.verify("alice", "whatever")
    msg = str(e.value)
    assert "127.0.0.1" not in msg and "alice" not in msg and "whatever" not in msg


def test_persisted_config_never_contains_a_secret():
    import json
    s = Source("shop", "mysql", {"host": "h", "user": "u", "password": "sekret", "database": "d"})
    persisted = json.loads(s.persisted_json())
    assert "password" not in persisted and "sekret" not in s.persisted_json()
    assert persisted == {"host": "h", "user": "u", "database": "d"}
    keep = Source("x", "mysql", {"host": "h", "user": "u", "password_env": "PW"})
    assert "password_env" in keep.persisted_json()


def test_source_connected_without_persisting_leaves_nothing_on_disk(svc):
    """The common case: `escrowe connect` attaches for this session only, and
    nothing - not even the host or username - is written unless asked."""
    svc.set_source(Source("other", "fake", {"host": "h", "user": "bob", "password": "bob"}), persist=False)
    assert svc.store.sources() == []
    assert svc.source().name == "other"


def test_a_source_stored_by_an_older_escrowe_still_connects(tmp_path):
    """A store from before the escrowe/passthrough/direct modes were removed
    may still have a `mode` key on a saved source; that store shouldn't need
    manual cleanup for escrowe to start."""
    import json
    from escrowe.config import Attachment, Settings
    from escrowe.service import Escrowe
    from escrowe.store import Store

    store = Store(tmp_path / "old.sqlite")
    store.save_source("fake", "fake", json.dumps(
        {"host": "h", "user": "bob", "password": "bob", "mode": "direct"}))
    settings = Settings(home=tmp_path, jwt_secret="t", attachments=[], llm_provider="mock")
    svc = Escrowe(settings, store=store)
    assert svc.engine is not None
