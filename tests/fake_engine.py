"""A tiny in-memory stand-in for a real database, used only by tests so the
suite doesn't need a live MySQL server for everything (see
tests/test_mysql_direct.py for the real-server tests).

It behaves like a real account-gated database: each account only sees the
tables it was granted, enforced by only creating those tables in that
account's own private connection - so querying an ungranted table fails the
same way it would against a real database, not because escrowe stepped in.
"""

from __future__ import annotations

import duckdb

from escrowe.engines import REGISTRY
from escrowe.engines.base import Column, DirectEngine, EngineError

TABLES = {
    "customers": {
        "columns": [("id", "INTEGER"), ("name", "VARCHAR"), ("tier", "VARCHAR")],
        "comment": "Customer master data",
        "rows": [(1, "Acme", "gold"), (2, "Globex", "silver"), (3, "Initech", "bronze")],
    },
    "employees": {
        "columns": [("id", "INTEGER"), ("name", "VARCHAR"), ("salary", "INTEGER")],
        "comment": "HR data. Salary is annual gross.",
        "rows": [(1, "Alice", 90000), (2, "Bob", 85000)],
    },
    "orders": {
        "columns": [("id", "INTEGER"), ("region", "VARCHAR")],
        "comment": None,
        "rows": [(i, "EU") for i in range(1, 1371)],
    },
}

ACCOUNTS = {
    "alice": {"password": "alice", "tables": {"customers", "orders"}},   # no employees: mimics no GRANT
    "bob": {"password": "bob", "tables": {"customers", "employees", "orders"}},
}


class FakeEngine(DirectEngine):
    kind = "fake"
    default_port = 0

    def __init__(self, host=None, user=None, password=None, port=0, database=None, **_):
        acct = ACCOUNTS.get(user)
        if acct is None or acct["password"] != password:
            raise EngineError("Access denied for user")
        self.user = user
        self.allowed = acct["tables"]
        self.conn = duckdb.connect(":memory:")
        for name in self.allowed:
            t = TABLES[name]
            cols_sql = ", ".join(f"{c} {typ}" for c, typ in t["columns"])
            self.conn.execute(f"CREATE TABLE {name} ({cols_sql})")
            for row in t["rows"]:
                self.conn.execute(f"INSERT INTO {name} VALUES ({', '.join('?' * len(row))})", row)
            if t["comment"]:
                self.conn.execute(f"COMMENT ON TABLE {name} IS '{t['comment']}'")

    @classmethod
    def test_login(cls, **params) -> None:
        cls(**params).close()

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    def catalog(self) -> list[Column]:
        rows = self.conn.execute(
            "SELECT c.table_name, c.column_name, c.data_type, t.comment "
            "FROM duckdb_columns() c LEFT JOIN duckdb_tables() t ON t.table_name = c.table_name "
            "WHERE NOT c.internal ORDER BY c.table_name, c.column_index").fetchall()
        return [Column(table.lower(), col, typ, None, comment)
                for table, col, typ, comment in rows]

    def table_sizes(self) -> dict[str, int]:
        return {name: len(TABLES[name]["rows"]) for name in self.allowed}

    def execute(self, sql: str, timeout_s: float | None = None):
        try:
            cur = self.conn.execute(sql)
            to_arrow = getattr(cur, "to_arrow_table", None) or cur.fetch_arrow_table
            return to_arrow()
        except Exception as e:
            raise EngineError(str(e).splitlines()[0][:300]) from e

    def diagnose(self) -> dict:
        return {"sources": [{"name": self.user, "kind": "fake"}], "tables": sorted(self.allowed)}


def register() -> None:
    REGISTRY.setdefault("fake", FakeEngine)
