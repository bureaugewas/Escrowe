"""A plain local SQLite file - no server, no per-account grants, just the
file directly. Whoever can open the file can do anything in it, the same
reasoning as engines/ducklake.py's DuckDBEngine: there is no login step for
this kind - see DirectEngine.requires_credentials."""

from __future__ import annotations

import threading

import pyarrow as pa

from .base import Column, DirectEngine, EngineError


class SQLiteEngine(DirectEngine):
    kind = "sqlite"
    default_port = None
    requires_credentials = False

    def __init__(self, path: str, **_):
        import sqlite3
        self._lock = threading.Lock()
        self.path = path
        try:
            self.conn = sqlite3.connect(path, check_same_thread=False)
        except Exception as e:
            raise EngineError(str(e).splitlines()[0][:300]) from e

    @classmethod
    def test_login(cls, **params) -> None:
        cls(**{k: v for k, v in params.items() if k == "path"}).close()

    def close(self) -> None:
        with self._lock:
            try:
                self.conn.close()
            except Exception:
                pass

    # -------------------------------------------------------------- catalog
    def catalog(self) -> list[Column]:
        with self._lock:
            cur = self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
            tables = [r[0] for r in cur.fetchall()]
            out = []
            for table in tables:
                for row in self.conn.execute(f"PRAGMA table_info({_q(table)})").fetchall():
                    # cid, name, type, notnull, dflt_value, pk
                    out.append(Column(table.lower(), row[1], row[2] or "", None, None))
        return out

    def table_sizes(self) -> dict[str, int]:
        with self._lock:
            cur = self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
            tables = [r[0] for r in cur.fetchall()]
            out = {}
            for table in tables:
                n = self.conn.execute(f"SELECT COUNT(*) FROM {_q(table)}").fetchone()[0]
                out[table.lower()] = int(n)
        return out

    def diagnose(self) -> dict:
        report: dict = {"sources": [{"name": self.path, "kind": "sqlite"}]}
        try:
            with self._lock:
                cur = self.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
                report["tables"] = [r[0] for r in cur.fetchall()]
        except Exception as e:
            report["error"] = str(e).splitlines()[0][:200]
        return report

    # ------------------------------------------------------------ execution
    def execute(self, sql: str, timeout_s: float | None = None) -> pa.Table:
        with self._lock:
            if timeout_s:
                try:
                    self.conn.execute(f"PRAGMA busy_timeout = {int(timeout_s * 1000)}")
                except Exception:
                    pass
            try:
                cur = self.conn.execute(sql)
                cols = [d[0] for d in (cur.description or [])]
                rows = cur.fetchall() if cur.description is not None else []
            except Exception as e:
                raise EngineError(str(e).splitlines()[0][:300]) from e
        arrays = [pa.array([r[i] for r in rows]) for i in range(len(cols))] if cols else []
        return pa.Table.from_arrays(arrays, names=cols) if cols else pa.table({})


def _q(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'
