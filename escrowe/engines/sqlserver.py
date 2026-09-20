"""SQL Server, connected directly: a real pymssql connection (pure-C TDS
client, no ODBC driver needed), opened with whatever account the person
configured, nothing in between. The account's own GRANTs are the only access
control; escrowe does not add or remove any."""

from __future__ import annotations

import threading

import pyarrow as pa

from .base import Column, DirectEngine, EngineError

SYSTEM_SCHEMAS = {"sys", "INFORMATION_SCHEMA", "guest", "db_owner", "db_accessadmin",
                  "db_securityadmin", "db_ddladmin", "db_backupoperator", "db_datareader",
                  "db_datawriter", "db_denydatareader", "db_denydatawriter"}


class SQLServerEngine(DirectEngine):
    kind = "sqlserver"
    default_port = 1433

    def __init__(self, host: str, user: str, password: str, port: int = 1433,
                 database: str | None = None, connect_timeout: float = 10.0):
        try:
            import pymssql
        except ImportError as e:
            raise EngineError("SQL Server support needs the 'pymssql' package (pip install pymssql).") from e
        self._lock = threading.Lock()
        self.database = database
        try:
            self.conn = pymssql.connect(server=host, user=user, password=password, port=int(port),
                                        database=database or "", login_timeout=int(connect_timeout),
                                        autocommit=True)
        except Exception as e:
            raise EngineError(str(e).splitlines()[0][:300]) from e

    @classmethod
    def test_login(cls, host: str, user: str, password: str, port: int = 1433,
                   database: str | None = None, **_) -> None:
        eng = cls(host=host, user=user, password=password, port=port, database=database)
        eng.close()

    def close(self) -> None:
        with self._lock:
            try:
                self.conn.close()
            except Exception:
                pass

    # -------------------------------------------------------------- catalog
    def catalog(self) -> list[Column]:
        with self._lock, self.conn.cursor() as cur:
            # No comment columns here - extended_properties needs a per-column
            # join too awkward to justify; comments are simply None, never invented.
            cur.execute(
                "SELECT c.TABLE_NAME, c.COLUMN_NAME, c.DATA_TYPE "
                "FROM INFORMATION_SCHEMA.COLUMNS c "
                "JOIN INFORMATION_SCHEMA.TABLES t "
                "  ON t.TABLE_SCHEMA = c.TABLE_SCHEMA AND t.TABLE_NAME = c.TABLE_NAME "
                "WHERE t.TABLE_TYPE = 'BASE TABLE' AND t.TABLE_SCHEMA NOT IN (" +
                ",".join("%s" for _ in SYSTEM_SCHEMAS) + ") "
                "ORDER BY c.TABLE_NAME, c.ORDINAL_POSITION", tuple(SYSTEM_SCHEMAS))
            rows = cur.fetchall()
        return [Column(table.lower(), col, typ, None, None) for table, col, typ in rows]

    def table_sizes(self) -> dict[str, int]:
        with self._lock, self.conn.cursor() as cur:
            cur.execute(
                "SELECT t.name, SUM(p.rows) FROM sys.tables t "
                "JOIN sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0, 1) "
                "GROUP BY t.name")
            return {name.lower(): int(rows) for name, rows in cur.fetchall() if rows is not None}

    def diagnose(self) -> dict:
        report: dict = {"sources": [{"name": self.database or "sqlserver", "kind": "sqlserver"}]}
        try:
            with self._lock, self.conn.cursor() as cur:
                cur.execute("SELECT TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                           "WHERE TABLE_TYPE = 'BASE TABLE'")
                report["tables"] = [tuple(r) for r in cur.fetchall()]
        except Exception as e:
            report["error"] = str(e).splitlines()[0][:200]
        return report

    # ------------------------------------------------------------ execution
    def execute(self, sql: str, timeout_s: float | None = None) -> pa.Table:
        with self._lock:
            if timeout_s:
                try:
                    self.conn._conn.query_timeout = int(timeout_s)
                except Exception:
                    pass
            with self.conn.cursor() as cur:
                try:
                    cur.execute(sql)
                except Exception as e:
                    raise EngineError(str(e).splitlines()[0][:300]) from e
                cols = [d[0] for d in (cur.description or [])]
                rows = cur.fetchall() if cur.description is not None else []
        arrays = [pa.array([r[i] for r in rows]) for i in range(len(cols))] if cols else []
        return pa.Table.from_arrays(arrays, names=cols) if cols else pa.table({})
