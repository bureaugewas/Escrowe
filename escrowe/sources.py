"""A Source: one database, connected directly, with a given account. The
account's own grants are the only access control - there is no escrowe-side
mode to choose, and no attach mechanism in between.

Kinds and parameters (see escrowe.engines for what each kind supports):
  mysql      host, port, user, password, database?
  ducklake   metadata, data_path?, token?   (see engines/ducklake.py - no
             per-account identity, so no user/password)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, unquote, urlparse

from . import engines
from .config import Attachment

SECRET_KEYS = ("password", "token")   # "token" authenticates a quack:-hosted DuckLake catalog


@dataclass
class Source:
    name: str
    kind: str
    params: dict = field(default_factory=dict)

    @classmethod
    def from_attachment(cls, a: Attachment) -> "Source":
        return parse_source_dsn(f"{a.kind}:{a.spec}", name=a.name)

    def redacted(self) -> dict:
        """Safe to log or return over the API."""
        p = {k: ("••••" if k in SECRET_KEYS and v else v) for k, v in self.params.items()}
        return {"name": self.name, "kind": self.kind, "params": p}

    def to_json(self) -> str:
        return json.dumps(self.params)

    def persisted_json(self) -> str:
        """What may be written to disk: never a literal secret (password, or a
        DuckLake/Quack token). A `password_env` reference is kept, so a source
        that resolves its secret from the environment still reconnects on its
        own - there is no equivalent env-indirection for a token today, so a
        ducklake source with a stripped token simply needs it re-entered."""
        clean = {k: v for k, v in self.params.items() if k not in SECRET_KEYS}
        return json.dumps(clean)


def resolve_secrets(p: dict) -> dict:
    """`password_env: DB_PW` reads the value from the environment at connect
    time, so the catalog file never holds the literal."""
    out = dict(p)
    env_key = out.pop("password_env", None)
    if env_key:
        value = os.environ.get(env_key)
        if value is None:
            raise ValueError(f"password_env points at ${env_key}, which is not set")
        out["password"] = value
    return out


DEFAULT_NAMES = {"mysql": "mysql", "ducklake": "lake"}


def _split_query(spec: str) -> tuple[str, dict]:
    if "?" not in spec:
        return spec, {}
    base, _, q = spec.rpartition("?")
    pairs = dict(parse_qsl(q))
    return (base, pairs) if pairs else (spec, {})


def _db_url_params(url: str) -> dict:
    """mysql://user:pw@host:port/dbname → {host, port, user, password, database}"""
    u = urlparse(url)
    p: dict = {"host": u.hostname or "127.0.0.1"}
    if u.port:
        p["port"] = u.port
    if u.username:
        p["user"] = unquote(u.username)
    if u.password:
        p["password"] = unquote(u.password)
    db = (u.path or "").lstrip("/")
    if db:
        p["database"] = db
    return p


def _kv_params(spec: str) -> dict:
    """host=127.0.0.1 user=ro password=x port=3306 database=shop  →  a dict,
    with port coerced to int when it parses as one."""
    out: dict = {}
    for tok in spec.split():
        k, sep, v = tok.partition("=")
        if not sep:
            continue
        out[k] = int(v) if k == "port" and v.isdigit() else v
    return out


def parse_source_dsn(dsn: str, name: str | None = None) -> Source:
    """One connection string per source.

        mysql://user:pw@host:3306/shop
        mysql:host=… user=… password=… database=…      key=value form
        ducklake:/path/to/catalog.duckdb
        ducklake:postgres:dbname=lake host=…            (DuckLake's own
        ducklake:quack:host:port                         ATTACH syntax,
                                                          passed through as-is)

    Any form takes ?data_path=… and, for ducklake, ?token=… .
    A leading `name=` sets the source name: `shop=mysql://user:pw@host/shop`.
    """
    text = dsn.strip()
    if not text:
        raise ValueError("empty connection string")

    head, sep, rest = text.partition(":")
    if "=" in head and sep:                       # name=kind:spec
        given_name, _, kind = head.partition("=")
        name = name or given_name.strip()
        text = f"{kind.strip()}:{rest}"

    kind, _, spec = text.partition(":")
    kind = kind.strip().lower()
    if kind not in engines.REGISTRY:
        raise ValueError(f"Unknown connection type {kind!r}; use one of {', '.join(engines.REGISTRY)} "
                         f"(e.g. mysql://user:pw@host:3306/shop)")
    spec = spec.strip()
    if not spec:
        raise ValueError(f"{kind} connection string needs a target, e.g. mysql://user:pw@host/shop")

    spec, extra = _split_query(spec)
    name = name or DEFAULT_NAMES.get(kind, kind)
    if kind == "ducklake":
        # Not a URL or key=value form: `spec` is DuckLake's own ATTACH
        # minilanguage (a path, or "postgres:...", "mysql:...", "quack:...").
        # escrowe doesn't parse it further - DuckLake's own ATTACH does.
        params = {"metadata": spec}
        if extra.get("token"):
            params["token"] = extra["token"]
    else:
        params = _db_url_params(spec) if "//" in spec else _kv_params(spec)
    if extra.get("data_path"):
        params["data_path"] = extra["data_path"]
    return Source(name, kind, params)


def server_attach_attempts(kind: str, p: dict) -> list[tuple[str, dict]]:
    """How to reach the *server* rather than one database, most likely first.

    A least-privilege account often cannot touch the server's own admin database
    (MySQL denies `mysql` to anyone without rights on it), so try with no
    database at all first.
    """
    base = {k: v for k, v in p.items() if k != "database"}
    if kind == "mysql":
        return [("no database", base),
                ("information_schema", {**base, "database": "information_schema"}),
                ("mysql", {**base, "database": "mysql"})]
    return [("no database", base)]


def list_databases(kind: str, params: dict, timeout_s: float = 10.0) -> list[str]:
    """Ask a server what this account can see, so setup can offer a choice
    instead of asking for a name blind. Raises with the server's reason."""
    if kind != "mysql":
        return []
    p = resolve_secrets(params)
    last = None
    for label, attempt in server_attach_attempts(kind, p):
        try:
            import pymysql
            conn = pymysql.connect(host=attempt.get("host", "127.0.0.1"), port=int(attempt.get("port", 3306)),
                                   user=attempt.get("user"), password=attempt.get("password"),
                                   database=attempt.get("database"), connect_timeout=timeout_s)
            with conn.cursor() as cur:
                cur.execute("SHOW DATABASES")
                rows = [r[0] for r in cur.fetchall()]
            conn.close()
        except Exception as e:
            last = e
            continue
        skip = {"information_schema", "performance_schema", "mysql", "sys"}
        return [r for r in rows if r not in skip]
    raise RuntimeError(str(last).splitlines()[0] if last else "could not reach the server")
