"""Who is this person? By opening a real connection to the configured
database with the credentials they typed. If it opens, they are who they say
they are, and their account's own grants are what everything after this runs
as. The password is used once and dropped - escrowe stores no hash and no
copy of it.
"""

from __future__ import annotations

from dataclasses import dataclass

from .engines import EngineError, REGISTRY


class AuthError(Exception):
    pass


@dataclass
class Identity:
    user: str
    verified_by: str


def _login_message(e: Exception) -> str:
    """Do not hand a caller the raw driver error: it leaks host detail."""
    text = str(e).lower()
    if "access denied" in text or "authentication" in text or "password" in text:
        return "Invalid user or password."
    if "unknown database" in text or "does not exist" in text:
        return "Invalid user or password."
    if "can't connect" in text or "connection" in text or "refused" in text or "timeout" in text:
        return "The database is not reachable, so the login could not be checked."
    return "Invalid user or password."


class DirectVerifier:
    """Proves identity by opening a connection through the source's own
    engine (see escrowe.engines). Works for any registered database kind."""

    def __init__(self, kind: str, params: dict):
        if kind not in REGISTRY:
            raise ValueError(f"Cannot verify logins against a {kind} source")
        self.kind, self.params = kind, params
        self.name = f"{kind}://{params.get('host', '?')}"

    def verify(self, user: str, password: str) -> Identity:
        p = {**self.params, "user": user, "password": password}
        p.pop("dsn", None)
        try:
            REGISTRY[self.kind].test_login(**p)
        except EngineError as e:
            raise AuthError(_login_message(e))
        return Identity(user.lower(), self.name)


def build_verifier(source) -> DirectVerifier:
    """`source` is the one configured escrowe.sources.Source."""
    return DirectVerifier(source.kind, source.params)
