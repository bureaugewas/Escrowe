"""A local SQLite file. There is no account: whoever can open the file can
do anything in it, so there is no login step (requires_credentials=False)."""

from __future__ import annotations

import sqlite3

from .base import Column, DBAPIEngine


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


class SQLiteEngine(DBAPIEngine):
    kind = "sqlite"
    requires_credentials = False

    def __init__(self, path: str, **_):
        super().__init__()
        self.path = path
        self._connect(sqlite3.connect, database=path, check_same_thread=False)

    def _tables(self) -> list[str]:
        rows = self._fetchall("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
        return [r[0] for r in rows]

    def catalog(self) -> list[Column]:
        # SQLite has no table or column comments; they stay None.
        out = []
        for table in self._tables():
            for _cid, name, typ, *_ in self._fetchall(f"PRAGMA table_info({_quote(table)})"):
                out.append(Column(table.lower(), name, typ or ""))
        return out

    def table_sizes(self) -> dict[str, int]:
        # SQLite keeps no row-count statistics, so this is a real COUNT(*) per
        # table. It reads no values, only how many rows there are.
        return {t.lower(): int(self._fetchall(f"SELECT COUNT(*) FROM {_quote(t)}")[0][0]) for t in self._tables()}

    def _apply_timeout(self, cur, timeout_s: float) -> None:
        cur.execute(f"PRAGMA busy_timeout = {int(timeout_s * 1000)}")
