"""Machine interface.

    import escrowe
    with escrowe.connect("escrowe://alice:alice@127.0.0.1:8765") as conn:
        r = conn.ask("revenue per region this quarter")
        r.sql        # what the agent wrote
        r.rows       # list of lists
        r.to_dict()  # {"sql": ..., "columns": [...], "rows": [...], ...}
        r.to_arrow() # pyarrow.Table
        conn.sql("SELECT ...")      # run SQL yourself, as your own account
        conn.metadata()             # the schema the agent sees

The connection stays open (HTTP keep-alive) until close(). If the session
token expires the client logs in again transparently.

The same interface is available in-process, without a server:

    conn = escrowe.client.embedded(settings)   # queries as whoever escrowe connected with
    conn = escrowe.client.embedded(settings).login("alice", "alice")   # or as yourself
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from urllib.parse import unquote, urlparse

import httpx


class EscroweError(Exception):
    """Transport or engine error."""


class EscroweDenied(EscroweError):
    """The database denied the query, or the agent refused. The message is user-safe."""


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
    def from_dict(cls, d: dict, question: str | None = None) -> "Result":
        return cls(sql=d.get("sql"), columns=d["columns"], rows=d["rows"], row_count=d["row_count"],
                   answer=d.get("answer"), duration_ms=d.get("duration_ms", 0.0),
                   audit_id=d.get("audit_id"), attempts=d.get("attempts", 1), provider=d.get("provider"),
                   question=question)

    def to_dict(self) -> dict:
        return {"question": self.question, "answer": self.answer, "sql": self.sql,
                "columns": self.columns, "rows": self.rows,
                "row_count": self.row_count, "duration_ms": self.duration_ms,
                "audit_id": self.audit_id, "attempts": self.attempts, "provider": self.provider}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str)

    def to_arrow(self):
        import pyarrow as pa
        return pa.table({c: [r[i] for r in self.rows] for i, c in enumerate(self.columns)}) \
            if self.columns else pa.table({})

    def to_pandas(self):
        return self.to_arrow().to_pandas()

    def records(self) -> list[dict]:
        return [dict(zip(self.columns, r)) for r in self.rows]


def parse_dsn(dsn: str) -> tuple[str, str | None, str | None]:
    """escrowe://user:password@host:port  →  (http://host:port, user, password).
    Plain http(s):// URLs are accepted too (user and password then come from arguments)."""
    u = urlparse(dsn)
    if u.scheme not in ("escrowe", "escrowes", "http", "https"):
        raise EscroweError(f"Unsupported scheme {u.scheme!r}; use escrowe://user:pass@host:port")
    scheme = {"escrowe": "http", "escrowes": "https"}.get(u.scheme, u.scheme)
    host = u.hostname or "127.0.0.1"
    port = u.port or (8765 if scheme == "http" else 443)
    return f"{scheme}://{host}:{port}", (unquote(u.username) if u.username else None), \
        (unquote(u.password) if u.password else None)


class Connection:
    """Client over HTTP. Keeps one keep-alive connection until close()."""

    def __init__(self, url: str, user: str | None = None, password: str | None = None,
                 timeout: float = 300.0, client: httpx.Client | None = None):
        self.url, self.user, self._password = url.rstrip("/"), user, password
        self._http = client or httpx.Client(base_url=self.url, timeout=timeout)
        self.token: str | None = None
        if user is not None and password is not None:
            self.login(user, password)

    # ------------------------------------------------------------ session
    def login(self, user: str, password: str) -> "Connection":
        r = self._http.post("/login", json={"user": user, "password": password})
        if r.status_code != 200:
            raise EscroweAuthError(_detail(r))
        d = r.json()
        self.user, self._password, self.token = d["sub"], password, d["token"]
        return self

    def close(self) -> None:
        """Ends the session: the server closes the connection it opened for you."""
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

    def _headers(self) -> dict:
        if not self.token:
            raise EscroweAuthError("Not logged in")
        return {"Authorization": f"Bearer {self.token}"}

    def _request(self, method: str, path: str, *, retry: bool = True, **kw) -> httpx.Response:
        try:
            r = self._http.request(method, path, headers=self._headers(), **kw)
        except httpx.HTTPError as e:
            raise EscroweError(f"Cannot reach Escrowe at {self.url}: {e}")
        if r.status_code == 401 and retry and self._password is not None:
            self.login(self.user, self._password)          # token expired: log in again once
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

    # ------------------------------------------------------------ queries
    def ask(self, question: str) -> Result:
        r = self._request("POST", "/ask", json={"question": question})
        return Result.from_dict(r.json(), question=question)

    def sql(self, sql: str) -> Result:
        r = self._request("POST", "/sql", json={"sql": sql})
        return Result.from_dict(r.json())

    def metadata(self) -> dict:
        return self._request("GET", "/metadata").json()

    def audit(self, limit: int = 50) -> list[dict]:
        return self._request("GET", "/audit", params={"limit": limit}).json()["rows"]

    # -------------------------------------------------------------- setup
    def attach(self, name: str, kind: str, params: dict) -> dict:
        return self._request("POST", "/sources", json={"name": name, "kind": kind, "params": params}).json()

    def sources(self) -> dict:
        return self._request("GET", "/sources").json()

    def detach(self, name: str) -> None:
        self._request("DELETE", f"/sources/{name}")

    def health(self) -> dict:
        return self._http.get("/health").json()


class LocalConnection:
    """Same interface, in-process: no server, no HTTP. Used by the interactive
    `escrowe` command on a machine that can reach the database directly."""

    def __init__(self, service, operator: bool = False):
        """operator=True queries as whoever escrowe itself connected with at
        startup, with no separate login. Only ever set by the local CLI."""
        from .service import Escrowe
        self.svc: Escrowe = service
        self.user = self.token = None
        self.operator = operator

    def login(self, user: str, password: str) -> "LocalConnection":
        from .service import AuthError
        try:
            d = self.svc.login(user, password)
        except AuthError as e:
            raise EscroweAuthError(str(e))
        self.user, self.token = d["sub"], d["token"]
        return self

    def _p(self):
        from .service import AuthError
        if self.token is None:
            if self.operator:
                return self.svc.operator_principal()
            raise EscroweAuthError("Not logged in")
        try:
            return self.svc.principal(self.token)
        except AuthError as e:
            raise EscroweAuthError(str(e))

    def _run(self, fn, *a, question=None, **kw):
        from .engines import EngineError
        from .guard import Denied
        try:
            return Result.from_dict(fn(*a, **kw).to_dict(), question=question)
        except Denied as e:
            denied = EscroweDenied(str(e))
            denied.needs_login = getattr(e, "needs_login", False)
            raise denied
        except EngineError as e:
            raise EscroweError(str(e))

    def ask(self, question: str, on_status=None, feed_data: str | None = None) -> Result:
        return self._run(self.svc.ask, self._p(), question, question=question,
                         on_status=on_status, feed_data=feed_data)

    def sql(self, sql: str) -> Result:
        return self._run(self.svc.sql, self._p(), sql)

    def metadata(self) -> dict:
        from .metadata import Metadata
        t = self.svc.catalog_for(self._p())
        if t is None:
            return {"tables": [], "text": ""}
        return {"tables": [x.to_dict() for x in t], "text": Metadata.render(t)}

    def audit(self, limit: int = 50) -> list[dict]:
        return [dict(r) for r in self.svc.store.audit_rows(None if self.operator else self.user, limit)]

    def attach(self, name: str, kind: str, params: dict, persist: bool = True) -> dict:
        from .engines import EngineError
        from .sources import Source
        try:
            return self.svc.set_source(Source(name, kind, params), persist=persist)
        except (ValueError, EngineError) as e:
            raise EscroweError(str(e))

    def sources(self) -> dict:
        return {"sources": [s.redacted() for s in self.svc.sources()]}

    def detach(self, name: str) -> None:
        self.svc.remove_source(name)

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
    """Open a connection: escrowe://user:password@host:port. Stays open until close()."""
    url, u, p = parse_dsn(dsn)
    return Connection(url, user or u, password or p, **kw)


def embedded(settings=None, operator: bool = False) -> LocalConnection:
    """In-process connection (no server). Call .login(user, password) next,
    or use it as-is to query as whoever escrowe connected with at startup."""
    from .config import load_settings
    from .service import Escrowe
    return LocalConnection(Escrowe(settings or load_settings()), operator=operator)


def _detail(r: httpx.Response) -> str:
    try:
        return r.json().get("detail", r.text)
    except Exception:
        return r.text
