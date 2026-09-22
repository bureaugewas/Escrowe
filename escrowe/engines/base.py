"""The interface every database engine implements, plus the shared plumbing
for engines whose driver follows the Python DB-API (PEP 249).

An engine does exactly three things: open a connection as a given account,
read the schema from the database's own system catalog, and run SQL. There
is no query rewriting and no escrowe-side permission model: the account the
engine connected with is the entire access decision.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

import pyarrow as pa


@dataclass(frozen=True)
class Column:
    """One column of one table, as the agent will see it."""

    fqn: str                     # what goes in a FROM clause: "table" or "schema.table"
    name: str
    type: str
    comment: str | None = None   # from the catalog, never invented
    table_comment: str | None = None


class EngineError(Exception):
    """A driver or connection error, reduced to one safe line of text."""


def first_line(e: Exception, limit: int = 300) -> str:
    return str(e).splitlines()[0][:limit] if str(e) else type(e).__name__


class DirectEngine:
    """Subclass this to add a database kind, then register it in engines/__init__.py."""

    kind: str = ""
    default_port: int | None = None
    # False for a kind with no per-account login (a local file, a DuckLake
    # catalog): escrowe connects straight from the configured params.
    requires_credentials: bool = True

    @classmethod
    def test_login(cls, **params) -> None:
        """Open and close a connection; raise EngineError if it cannot be opened."""
        cls(**params).close()

    def close(self) -> None:
        raise NotImplementedError

    def catalog(self) -> list[Column]:
        """Table and column names, types and comments. Must read only the
        database's own system catalog, never a user table."""
        raise NotImplementedError

    def table_sizes(self) -> dict[str, int]:
        """Approximate row counts by fqn, from catalog statistics."""
        raise NotImplementedError

    def execute(self, sql: str, timeout_s: float | None = None) -> pa.Table:
        raise NotImplementedError


class DBAPIEngine(DirectEngine):
    """Shared connection handling for drivers that follow PEP 249.

    A subclass sets `self.conn` in its own __init__ (via `_connect`) and
    implements `catalog`/`table_sizes`. Optionally it overrides
    `_apply_timeout` to translate a per-query timeout into the database's
    own mechanism.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.conn = None

    def _connect(self, opener, **kwargs) -> None:
        try:
            self.conn = opener(**kwargs)
        except Exception as e:
            raise EngineError(first_line(e)) from e

    def close(self) -> None:
        with self._lock:
            try:
                self.conn.close()
            except Exception:
                pass

    def _apply_timeout(self, cur, timeout_s: float) -> None:
        """Best effort: not every server build supports a statement timeout."""

    def execute(self, sql: str, timeout_s: float | None = None) -> pa.Table:
        with self._lock:
            cur = self.conn.cursor()
            try:
                if timeout_s:
                    try:
                        self._apply_timeout(cur, timeout_s)
                    except Exception:
                        pass
                try:
                    cur.execute(sql)
                except Exception as e:
                    raise EngineError(first_line(e)) from e
                cols = [d[0] for d in (cur.description or [])]
                rows = cur.fetchall() if cur.description is not None else []
            finally:
                cur.close()
        return rows_to_arrow(cols, rows)

    def _fetchall(self, sql: str, params=None) -> list[tuple]:
        """Run a fixed catalog query. Only ever called with SQL written in
        this package, never with anything the agent or a person typed."""
        with self._lock:
            cur = self.conn.cursor()
            try:
                cur.execute(sql, params) if params is not None else cur.execute(sql)
                return [tuple(r) for r in cur.fetchall()]
            finally:
                cur.close()


def rows_to_arrow(cols: list[str], rows: list[tuple]) -> pa.Table:
    if not cols:
        return pa.table({})
    return pa.Table.from_arrays([pa.array([r[i] for r in rows]) for i in range(len(cols))], names=cols)
