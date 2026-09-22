"""The Python interface, in two flavours with the same methods.

Over HTTP, against `escrowe serve`:

    import escrowe
    with escrowe.connect("escrowe://alice:pw@host:8765") as conn:
        r = conn.ask("revenue per region this quarter")
        r.sql, r.rows, r.to_dict(), r.to_arrow(), r.to_pandas()
        conn.sql("SELECT ...")
        conn.metadata()

In-process, with no server (what the CLI uses):

    conn = escrowe.embedded()                       # as the account escrowe connected with
    conn = escrowe.embedded().login("alice", "pw")  # or as yourself
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

import httpx


class EscroweError(Exception):
    """Transport or engine error."""


class EscroweDenied(EscroweError):
    """The guard denied the query, or the agent refused. The message is user-safe."""
    needs_login: bool = False


class EscroweAuthError(EscroweError):
    pass


@dataclass
class Result:
    sql: str | None
    columns: list[str]
    rows: list[list]
    row_count: int
    duration_ms: float = 0.0
    audit_id: int | None = None
    attempts: int = 1
    provider: str | None = None
    question: str | None = None
    answer: str | None = None        # set when the agent replied in words, not SQL

    @classmethod
    def from_dict(cls, d: dict, question: str | None = None) -> Result:
        return cls(sql=d.get("sql"), columns=d["columns"], rows=d["rows"], row_count=d["row_count"],
                   answer=d.get("answer"), duration_ms=d.get("duration_ms", 0.0), audit_id=d.get("audit_id"),
                   attempts=d.get("attempts", 1), provider=d.get("provider"), question=question)

    def to_dict(self) -> dict:
        return {"question": self.question, "answer": self.answer, "sql": self.sql,
                "columns": self.columns, "rows": self.rows, "row_count": self.row_count,
                "duration_ms": self.duration_ms, "audit_id": self.audit_id,
                "attempts": self.attempts, "provider": self.provider}

    def to_json(self) -> str:
        """For people and scripts: each row as a {column: value} object."""
        d = {k: v for k, v in self.to_dict().items() if k != "columns"}
        return json.dumps({**d, "rows": self.records()}, default=str)

    def to_arrow(self):
        import pyarrow as pa
        if not self.columns:
            return pa.table({})
        return pa.table({c: [r[i] for r in self.rows] for i, c in enumerate(self.columns)})

    def to_pandas(self):
        return self.to_arrow().to_pandas()

    def records(self) -> list[dict]:
        return [dict(zip(self.columns, r, strict=False)) for r in self.rows]


def parse_dsn(dsn: str) -> tuple[str, str | None, str | None]:
    """escrowe://user:password@host:port -> (http://host:port, user, password)."""
    u = urlparse(dsn)
    if u.scheme not in ("escrowe", "escrowes", "http", "https"):
        raise EscroweError(f"Unsupported scheme {u.scheme!r}; use escrowe://user:pass@host:port")
    scheme = {"escrowe": "http", "escrowes": "https"}.get(u.scheme, u.scheme)
    port = u.port or (8765 if scheme == "http" else 443)
    return (f"{scheme}://{u.hostname or '127.0.0.1'}:{port}",
            unquote(u.username) if u.username else None,
            unquote(u.password) if u.password else None)


def _detail(r: httpx.Response) -> str:
    try:
        return r.json().get("detail", r.text)
    except Exception:
        return r.text


class Connection:
    """HTTP client. Keeps one keep-alive connection until close(); if the
    session token expires it logs in again transparently."""

    def __init__(self, url: str, user: str | None = None, password: str | None = None,
                 timeout: float = 300.0, client: httpx.Client | None = None):
        self.url, self.user, self._password = url.rstrip("/"), user, password
        self._http = client or httpx.Client(base_url=self.url, timeout=timeout)
        self.token: str | None = None
        if user is not None and password is not None:
            self.login(user, password)

    def login(self, user: str, password: str) -> Connection:
        try:
            r = self._http.post("/login", json={"user": user, "password": password})
        except httpx.HTTPError as e:
            raise EscroweError(f"Cannot reach Escrowe at {self.url}: {e}")
        if r.status_code != 200:
            raise EscroweAuthError(_detail(r))
        d = r.json()
        self.user, self._password, self.token = d["sub"], password, d["token"]
        return self

    def close(self) -> None:
        if self.token:
            try:
                self._http.post("/logout", headers={"Authorization": f"Bearer {self.token}"})
            except Exception:
                pass
            self.token = None
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _request(self, method: str, path: str, *, retry: bool = True, **kw) -> httpx.Response:
        if not self.token:
            raise EscroweAuthError("Not logged in")
        try:
            r = self._http.request(method, path, headers={"Authorization": f"Bearer {self.token}"}, **kw)
        except httpx.HTTPError as e:
            raise EscroweError(f"Cannot reach Escrowe at {self.url}: {e}")
        if r.status_code == 401 and retry and self._password is not None:
            self.login(self.user, self._password)
            return self._request(method, path, retry=False, **kw)
        if r.status_code == 401:
            raise EscroweAuthError(_detail(r))
        if r.status_code == 403:
            denied = EscroweDenied(_detail(r))
            denied.needs_login = r.headers.get("X-Escrowe-Needs-Login") == "1"
            raise denied
        if r.status_code >= 400:
            raise EscroweError(_detail(r))
        return r

    def ask(self, question: str, **_) -> Result:
        return Result.from_dict(self._request("POST", "/ask", json={"question": question}).json(), question)

    def sql(self, sql: str) -> Result:
        return Result.from_dict(self._request("POST", "/sql", json={"sql": sql}).json())

    def metadata(self) -> dict:
        return self._request("GET", "/metadata").json()

    def audit(self, limit: int = 50) -> list[dict]:
        return self._request("GET", "/audit", params={"limit": limit}).json()["rows"]

    def source(self) -> dict | None:
        sources = self._request("GET", "/sources").json()["sources"]
        return sources[0] if sources else None

    def set_source(self, name: str, kind: str, params: dict, persist: bool = True) -> dict:
        return self._request("POST", "/sources", json={"name": name, "kind": kind, "params": params}).json()

    def remove_source(self) -> None:
        self._request("DELETE", "/sources/current")

    def health(self) -> dict:
        return self._http.get("/health").json()


class LocalConnection:
    """The same interface, in-process. `operator=True` queries as the account
    escrowe itself connected with, without a separate login; only the local
    CLI sets it."""

    def __init__(self, service, operator: bool = False):
        self.svc = service
        self.user: str | None = None
        self.token: str | None = None
        self.operator = operator

    def login(self, user: str, password: str) -> LocalConnection:
        from .service import AuthError
        try:
            d = self.svc.login(user, password)
        except AuthError as e:
            raise EscroweAuthError(str(e))
        self.user, self.token = d["sub"], d["token"]
        return self

    def principal(self):
        from .service import AuthError
        if self.token is None:
            if self.operator:
                return self.svc.operator_principal()
            raise EscroweAuthError("Not logged in")
        try:
            return self.svc.principal(self.token)
        except AuthError as e:
            raise EscroweAuthError(str(e))

    def _run(self, fn, *args, question=None, **kw) -> Result:
        from .engines import EngineError
        from .guard import Denied
        from .service import AuthError
        try:
            return Result.from_dict(fn(*args, **kw).to_dict(), question=question)
        except Denied as e:
            denied = EscroweDenied(str(e))
            denied.needs_login = getattr(e, "needs_login", False)
            raise denied
        except AuthError as e:
            raise EscroweAuthError(str(e))
        except EngineError as e:
            raise EscroweError(str(e))

    def ask(self, question: str, on_status=None, feed_data: str | None = None, on_token=None) -> Result:
        return self._run(self.svc.ask, self.principal(), question, question=question,
                         on_status=on_status, feed_data=feed_data, on_token=on_token)

    def sql(self, sql: str) -> Result:
        return self._run(self.svc.sql, self.principal(), sql)

    def metadata(self) -> dict:
        from .metadata import render_schema
        tables = self.svc.schema_for(self.principal()) or []
        return {"tables": [t.to_dict() for t in tables], "text": render_schema(tables)}

    def audit(self, limit: int = 50) -> list[dict]:
        return [dict(r) for r in self.svc.store.audit_rows(None if self.operator else self.user, limit)]

    def source(self) -> dict | None:
        return self.svc.source.redacted() if self.svc.source else None

    def set_source(self, name: str, kind: str, params: dict, persist: bool = True) -> dict:
        from .engines import EngineError
        from .sources import Source
        try:
            return self.svc.set_source(Source(name, kind, params), persist=persist)
        except (ValueError, EngineError) as e:
            raise EscroweError(str(e))

    def remove_source(self) -> None:
        self.svc.remove_source()

    def health(self) -> dict:
        return {"ok": True, "embedded": True, "agent": self.svc.agent.status()}

    def close(self) -> None:
        if self.token:
            self.svc.logout(self.token)
            self.token = None
        if self.svc.engine is not None:
            self.svc.engine.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def connect(dsn: str = "escrowe://127.0.0.1:8765", user: str | None = None, password: str | None = None,
            **kw) -> Connection:
    """Open a connection to an escrowe server. Stays open until close()."""
    url, u, p = parse_dsn(dsn)
    return Connection(url, user or u, password or p, **kw)


def embedded(settings=None, operator: bool = False) -> LocalConnection:
    """In-process connection. Call .login(user, password) next, or pass
    operator=True to query as the account escrowe connected with."""
    from .config import load_settings
    from .service import Escrowe
    return LocalConnection(Escrowe(settings or load_settings()), operator=operator)
