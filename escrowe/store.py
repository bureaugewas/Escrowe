"""Escrowe's own tiny bit of state: which source is configured, app settings
(which LLM, its JWT secret), and the audit log of every query. SQLite.

There is no user table, no roles, no grants, no policies: access is decided
entirely by the database escrowe is connected to (its own users/grants), not
by escrowe. See CLAUDE.md / README for why.
"""

from __future__ import annotations

import secrets
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sources (
    name TEXT PRIMARY KEY, kind TEXT NOT NULL, params TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    user TEXT, role TEXT, mode TEXT, question TEXT, candidate_sql TEXT, compiled_sql TEXT,
    decision TEXT, reason TEXT, row_count INTEGER, duration_ms REAL, attempts INTEGER);
"""


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        try:                       # holds a JWT secret and the audit log: keep it to this user
            import os
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # -------------------------------------------------------------- helpers
    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _rows(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # ------------------------------------------------------------- settings
    def setting(self, key: str) -> str | None:
        r = self._rows("SELECT value FROM settings WHERE key = ?", (key,))
        return r[0]["value"] if r else None

    def set_setting(self, key: str, value: str) -> None:
        self._exec("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))

    def jwt_secret(self, configured: str | None) -> str:
        if configured:
            return configured
        s = self.setting("jwt_secret")
        if not s:
            s = secrets.token_hex(32)
            self.set_setting("jwt_secret", s)
        return s

    # -------------------------------------------------------------- sources
    def save_source(self, name: str, kind: str, params_json: str) -> None:
        self._exec("INSERT OR REPLACE INTO sources VALUES (?, ?, ?)", (name, kind, params_json))

    def delete_source(self, name: str) -> None:
        self._exec("DELETE FROM sources WHERE name = ?", (name,))

    def clear_sources(self) -> None:
        self._exec("DELETE FROM sources")

    def sources(self) -> list[sqlite3.Row]:
        return self._rows("SELECT * FROM sources ORDER BY name")

    # ---------------------------------------------------------------- audit
    def audit(self, **fields) -> int:
        cols = ", ".join(fields)
        marks = ", ".join("?" * len(fields))
        return self._exec(f"INSERT INTO audit ({cols}) VALUES ({marks})",
                          tuple(fields.values())).lastrowid

    def audit_rows(self, user: str | None = None, limit: int = 100) -> list[sqlite3.Row]:
        if user:
            return self._rows("SELECT * FROM audit WHERE user=? ORDER BY id DESC LIMIT ?", (user, limit))
        return self._rows("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))
