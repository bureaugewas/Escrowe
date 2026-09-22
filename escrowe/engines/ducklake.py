"""DuckDB, in two flavours that share everything but the connect step.

DuckDBEngine   a plain local .duckdb file.
DuckLakeEngine a DuckLake catalog, attached through DuckDB's own `ducklake`
               extension. The catalog can live in a local file, SQLite,
               Postgres, MySQL, or a hosted Quack server; `metadata` is
               passed straight to DuckLake's ATTACH syntax:

                 /path/to/catalog.duckdb
                 sqlite:/path/to/catalog.sqlite
                 postgres:dbname=... host=... user=... password=...
                 quack:host:port          (with `token` to authenticate)

               `data_path` is only needed the first time a catalog is used.

Neither has a per-account login: whoever can open the file or catalog can
do anything in it, so requires_credentials is False.
"""

from __future__ import annotations

import threading

import pyarrow as pa

from .base import Column, DirectEngine, EngineError, first_line


def _lit(v: str) -> str:
    return "'" + str(v).replace("'", "''") + "'"


class DuckDBEngine(DirectEngine):
    kind = "duckdb"
    requires_credentials = False

    def __init__(self, path: str, **_):
        self._lock = threading.Lock()
        self.conn = self._open(path)
        self.alias = self.conn.execute("SELECT current_database()").fetchone()[0]

    @staticmethod
    def _open(path: str):
        try:
            import duckdb
        except ImportError as e:
            raise EngineError("DuckDB support needs the 'duckdb' package.") from e
        try:
            return duckdb.connect(path)
        except Exception as e:
            raise EngineError(first_line(e)) from e

    def close(self) -> None:
        with self._lock:
            try:
                self.conn.close()
            except Exception:
                pass

    def _fqn(self, schema: str, table: str) -> str:
        return table if schema == "main" else f"{schema}.{table}"

    def catalog(self) -> list[Column]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT c.schema_name, c.table_name, c.column_name, c.data_type, c.comment, t.comment "
                "FROM duckdb_columns() c LEFT JOIN duckdb_tables() t "
                "  ON t.database_name = c.database_name AND t.schema_name = c.schema_name "
                "     AND t.table_name = c.table_name "
                "WHERE c.database_name = ? AND NOT c.internal "
                "ORDER BY c.schema_name, c.table_name, c.column_index", [self.alias]).fetchall()
        return [Column(self._fqn(schema, table), col, typ, ccomment or None, tcomment or None)
                for schema, table, col, typ, ccomment, tcomment in rows]

    def table_sizes(self) -> dict[str, int]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT schema_name, table_name, estimated_size FROM duckdb_tables() "
                "WHERE database_name = ? AND NOT internal", [self.alias]).fetchall()
        return {self._fqn(schema, table): int(size) for schema, table, size in rows if size is not None}

    def execute(self, sql: str, timeout_s: float | None = None) -> pa.Table:
        with self._lock:
            timer = threading.Timer(timeout_s, self.conn.interrupt) if timeout_s else None
            if timer:
                timer.start()
            try:
                cur = self.conn.execute(sql)
                to_arrow = getattr(cur, "to_arrow_table", None) or cur.fetch_arrow_table
                return to_arrow()
            except Exception as e:
                raise EngineError(first_line(e)) from e
            finally:
                if timer:
                    timer.cancel()


class DuckLakeEngine(DuckDBEngine):
    kind = "ducklake"

    def __init__(self, metadata: str, data_path: str | None = None, token: str | None = None,
                 alias: str = "lake", **_):
        self._lock = threading.Lock()
        self.alias = alias
        self.conn = self._open(":memory:")
        try:
            self.conn.execute("INSTALL ducklake; LOAD ducklake;")
            if token:
                # The secret's SCOPE must match the quack: host or DuckDB ignores it.
                scope = ""
                if metadata.startswith("quack:"):
                    host = metadata[len("quack:"):].split(":")[0]
                    scope = f", SCOPE {_lit('quack:' + host)}"
                self.conn.execute(f"CREATE SECRET escrowe_quack (TYPE quack, TOKEN {_lit(token)}{scope})")
            options = f" (DATA_PATH {_lit(data_path)})" if data_path else ""
            self.conn.execute(f"ATTACH {_lit('ducklake:' + metadata)} AS {alias}{options}")
            self.conn.execute(f"USE {alias}")
        except Exception as e:
            self.close()
            raise EngineError(first_line(e)) from e
