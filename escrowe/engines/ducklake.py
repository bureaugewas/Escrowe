"""DuckLake, connected directly through DuckDB's own `ducklake` extension -
this is the one kind where DuckDB is unavoidable, because DuckLake *is* a
DuckDB catalog/table format, not a separate server escrowe could talk to on
its own. escrowe uses DuckDB here purely as DuckLake's client library, the
same way engines/mysql.py uses PyMySQL as MySQL's.

A DuckLake catalog (metadata: which tables, which columns, which Parquet
files belong to which table) can itself live in a few places, and escrowe
doesn't try to parse or specialize the connection string - it passes
whatever you give it straight to DuckLake's own ATTACH syntax:

  metadata="/path/to/catalog.duckdb"                     a local DuckDB file
  metadata="sqlite:/path/to/catalog.sqlite"               a local SQLite file
  metadata="postgres:dbname=... host=... user=... password=..."
  metadata="mysql:host=... user=... password=... database=..."
  metadata="quack:host:port"                              a hosted Quack server

`data_path` is where the actual Parquet data lives; DuckLake only needs it
the first time a given catalog is used (to create it) - attaching an
*existing* DuckLake reads it back from the catalog itself, so it's optional.
`token` authenticates a `quack:`-hosted catalog.

DuckLake has no per-account grants of its own (unlike MySQL): whoever can
open the catalog can do anything in it. There is no login step for this
kind - see DirectEngine.requires_credentials.
"""

from __future__ import annotations

import threading

import pyarrow as pa

from .base import Column, DirectEngine, EngineError

SYSTEM_DBS = {"system", "temp", "memory"}     # duckdb's own built-ins, not the lake


class DuckLakeEngine(DirectEngine):
    kind = "ducklake"
    default_port = None
    requires_credentials = False   # no username/password - see module docstring

    def __init__(self, metadata: str, data_path: str | None = None, token: str | None = None,
                 alias: str = "lake", **_):
        try:
            import duckdb
        except ImportError as e:
            raise EngineError("DuckLake support needs the 'duckdb' package (pip install duckdb).") from e
        self._lock = threading.Lock()
        self.alias = alias
        self.conn = duckdb.connect(":memory:")
        try:
            self.conn.execute("INSTALL ducklake; LOAD ducklake;")
            if token:
                # Named so a second ATTACH in the same process doesn't collide;
                # only meaningful for a quack:-hosted catalog. SCOPE must match
                # the host in `metadata` (quack:host:port) or DuckDB won't use
                # this secret to authenticate the ATTACH at all.
                scope_sql = ""
                if metadata.startswith("quack:"):
                    host = metadata[len("quack:"):].split(":")[0]
                    scope_sql = f", SCOPE {_q(f'quack:{host}')}"
                self.conn.execute(
                    f"CREATE SECRET escrowe_ducklake_quack (TYPE quack, TOKEN {_q(token)}{scope_sql})")
            opts = [f"DATA_PATH {_q(data_path)}"] if data_path else []
            opts_sql = f" ({', '.join(opts)})" if opts else ""
            self.conn.execute(f"ATTACH {_q('ducklake:' + metadata)} AS {alias}{opts_sql}")
            self.conn.execute(f"USE {alias}")   # so table names need no prefix, like the mysql engine
        except Exception as e:
            try:
                self.conn.close()
            except Exception:
                pass
            raise EngineError(str(e).splitlines()[0][:300]) from e

    @classmethod
    def test_login(cls, **params) -> None:
        cls(**{k: v for k, v in params.items() if k in ("metadata", "data_path", "token", "alias")}).close()

    def close(self) -> None:
        with self._lock:
            try:
                self.conn.close()
            except Exception:
                pass

    # -------------------------------------------------------------- catalog
    def catalog(self) -> list[Column]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT c.schema_name, c.table_name, c.column_name, c.data_type, c.comment, t.comment "
                "FROM duckdb_columns() c LEFT JOIN duckdb_tables() t "
                "  ON t.database_name = c.database_name AND t.schema_name = c.schema_name "
                "     AND t.table_name = c.table_name "
                f"WHERE c.database_name = '{self.alias}' AND NOT c.internal "
                "ORDER BY c.schema_name, c.table_name, c.column_index").fetchall()
        return [Column(table if schema == "main" else f"{schema}.{table}", col, typ,
                       ccomment or None, tcomment or None)
                for schema, table, col, typ, ccomment, tcomment in rows]

    def table_sizes(self) -> dict[str, int]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT schema_name, table_name, estimated_size FROM duckdb_tables() "
                f"WHERE database_name = '{self.alias}' AND NOT internal").fetchall()
        out = {}
        for schema, table, size in rows:
            if size is None:
                continue
            out[table if schema == "main" else f"{schema}.{table}"] = int(size)
        return out

    def diagnose(self) -> dict:
        report: dict = {"sources": [{"name": self.alias, "kind": "ducklake"}]}
        try:
            with self._lock:
                report["databases"] = self.conn.execute(
                    "SELECT database_name, type FROM duckdb_databases()").fetchall()
                report["tables"] = self.conn.execute(
                    "SELECT schema_name, table_name FROM duckdb_tables() "
                    f"WHERE database_name = '{self.alias}' AND NOT internal LIMIT 200").fetchall()
        except Exception as e:
            report["error"] = str(e).splitlines()[0][:200]
        return report

    # ------------------------------------------------------------ execution
    def execute(self, sql: str, timeout_s: float | None = None) -> pa.Table:
        with self._lock:
            timer = None
            if timeout_s:
                import threading as _t
                timer = _t.Timer(timeout_s, self.conn.interrupt)
                timer.start()
            try:
                cur = self.conn.execute(sql)
                to_arrow = getattr(cur, "to_arrow_table", None) or cur.fetch_arrow_table
                return to_arrow()
            except Exception as e:
                raise EngineError(str(e).splitlines()[0][:300]) from e
            finally:
                if timer:
                    timer.cancel()


class DuckDBEngine(DuckLakeEngine):
    """A plain local DuckDB file - no DuckLake catalog format, no lake server,
    just the file directly. Subclasses DuckLakeEngine purely for its catalog()/
    table_sizes()/execute()/close(): none of those are DuckLake-specific, they
    just read duckdb_tables()/duckdb_columns() filtered by self.alias and run
    SQL over self.conn, which is exactly as true for a plain file. Only the
    connect step itself differs (no ducklake extension, no ATTACH)."""
    kind = "duckdb"

    def __init__(self, path: str, **_):
        try:
            import duckdb
        except ImportError as e:
            raise EngineError("DuckDB support needs the 'duckdb' package (pip install duckdb).") from e
        self._lock = threading.Lock()
        try:
            self.conn = duckdb.connect(path)
            self.alias = self.conn.execute("SELECT current_database()").fetchone()[0]
        except Exception as e:
            raise EngineError(str(e).splitlines()[0][:300]) from e

    @classmethod
    def test_login(cls, **params) -> None:
        cls(**{k: v for k, v in params.items() if k == "path"}).close()


def _q(v: str) -> str:
    return "'" + str(v).replace("'", "''") + "'"
