"""A Source is the one database escrowe is connected to: a kind (mysql,
postgres, ...) and the parameters its engine needs. This module parses
connection strings into Sources and decides what may be written to disk.

Connection string forms accepted by `parse_dsn`:

    mysql://user:pw@host:3306/shop
    postgres://user:pw@host/shop
    sqlserver://user:pw@host/shop
    mysql:host=... user=... password=... database=...    key=value form
    sqlite:/path/to/file.sqlite
    duckdb:/path/to/file.duckdb
    ducklake:/path/to/catalog.duckdb
    ducklake:postgres:dbname=lake host=...                 DuckLake's own ATTACH syntax
    ducklake:quack:host:port?token=...

Any form takes `?data_path=...`. A leading `name=` names the source.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, unquote, urlparse

from . import engines

SECRET_KEYS = ("password", "token")   # a token authenticates a hosted DuckLake catalog
DEFAULT_NAMES = {"ducklake": "lake"}
FILE_KINDS = ("sqlite", "duckdb")


@dataclass
class Source:
    name: str
    kind: str
    params: dict = field(default_factory=dict)

    @property
    def user(self) -> str | None:
        return self.params.get("user")

    def redacted(self) -> dict:
        """Safe to log or return over the API."""
        params = {k: ("••••" if k in SECRET_KEYS and v else v) for k, v in self.params.items()}
        return {"name": self.name, "kind": self.kind, "params": params}

    def persisted_json(self) -> str:
        """What may be written to disk: everything except a secret. A source
        loaded back from disk therefore needs its password supplied again."""
        return json.dumps({k: v for k, v in self.params.items() if k not in SECRET_KEYS})

    @classmethod
    def from_row(cls, name: str, kind: str, params_json: str) -> Source:
        return cls(name, kind, json.loads(params_json))


def _split_query(spec: str) -> tuple[str, dict]:
    if "?" not in spec:
        return spec, {}
    base, _, query = spec.rpartition("?")
    pairs = dict(parse_qsl(query))
    return (base, pairs) if pairs else (spec, {})


def _url_params(url: str) -> dict:
    """mysql://user:pw@host:port/dbname -> {host, port, user, password, database}"""
    u = urlparse(url)
    params: dict = {"host": u.hostname or "127.0.0.1"}
    if u.port:
        params["port"] = u.port
    if u.username:
        params["user"] = unquote(u.username)
    if u.password:
        params["password"] = unquote(u.password)
    if (u.path or "").lstrip("/"):
        params["database"] = u.path.lstrip("/")
    return params


def _kv_params(spec: str) -> dict:
    """host=127.0.0.1 user=ro password=x port=3306 database=shop -> a dict"""
    params: dict = {}
    for token in spec.split():
        key, sep, value = token.partition("=")
        if sep:
            params[key] = int(value) if key == "port" and value.isdigit() else value
    return params


def parse_dsn(dsn: str, name: str | None = None) -> Source:
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
        raise ValueError(f"Unknown database kind {kind!r}; use one of {', '.join(engines.REGISTRY)}")
    spec = spec.strip()
    if not spec:
        raise ValueError(f"{kind} connection string needs a target, e.g. {kind}://user:pw@host/db")

    spec, extra = _split_query(spec)
    if kind == "ducklake":
        params = {"metadata": spec}
        if extra.get("token"):
            params["token"] = extra["token"]
    elif kind in FILE_KINDS:
        params = {"path": spec.removeprefix("//")}
    else:
        params = _url_params(spec) if "//" in spec else _kv_params(spec)
    if extra.get("data_path"):
        params["data_path"] = extra["data_path"]
    return Source(name or DEFAULT_NAMES.get(kind, kind), kind, params)


def list_mysql_databases(params: dict, timeout_s: float = 10.0) -> list[str]:
    """What this MySQL account can see, so setup can offer a choice. Tries
    without a database first: a least-privilege account often may not open
    the server's own admin databases."""
    import pymysql

    from .engines.mysql import SYSTEM_DBS

    base = {k: v for k, v in params.items() if k != "database"}
    last: Exception | None = None
    for database in (None, "information_schema", "mysql"):
        try:
            conn = pymysql.connect(host=base.get("host", "127.0.0.1"), port=int(base.get("port", 3306)),
                                   user=base.get("user"), password=base.get("password"),
                                   database=database, connect_timeout=timeout_s)
            try:
                with conn.cursor() as cur:
                    cur.execute("SHOW DATABASES")
                    return [r[0] for r in cur.fetchall() if r[0] not in SYSTEM_DBS]
            finally:
                conn.close()
        except Exception as e:
            last = e
    raise RuntimeError(str(last).splitlines()[0] if last else "could not reach the server")
