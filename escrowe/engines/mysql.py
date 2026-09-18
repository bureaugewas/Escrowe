"""MySQL/MariaDB, connected directly: a real PyMySQL connection, opened with
whatever account the person configured, nothing in between. The account's own
GRANTs are the only access control; escrowe does not add or remove any."""

from __future__ import annotations

import threading

import pyarrow as pa

from .base import Column, DirectEngine, EngineError

SYSTEM_DBS = {"information_schema", "mysql", "performance_schema", "sys"}


class MySQLEngine(DirectEngine):
    kind = "mysql"
    default_port = 3306

    def __init__(self, host: str, user: str, password: str, port: int = 3306,
                 database: str | None = None, connect_timeout: float = 10.0):
        try:
            import pymysql
        except ImportError as e:
            raise EngineError("MySQL support needs the 'pymysql' package (pip install pymysql).") from e
        self._lock = threading.Lock()
        self.database = database          # when set, every fqn is the bare table name: it's
                                          # already what this connection resolves unqualified
        try:
            self.conn = pymysql.connect(host=host, user=user, password=password, port=int(port),
                                        database=database, connect_timeout=connect_timeout,
                                        cursorclass=pymysql.cursors.Cursor)
        except Exception as e:
            raise EngineError(str(e).splitlines()[0][:300]) from e

    @classmethod
    def test_login(cls, host: str, user: str, password: str, port: int = 3306,
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
    def _databases(self) -> list[str]:
        with self._lock, self.conn.cursor() as cur:
            cur.execute("SHOW DATABASES")
            return [r[0] for r in cur.fetchall() if r[0] not in SYSTEM_DBS]

    def _fqn(self, db: str, table: str) -> str:
        # Connected to one database already: an unqualified table name is all
        # a query needs. Only a whole-server connection needs db.table.
        return table.lower() if self.database else f"{db}.{table}".lower()

    def catalog(self) -> list[Column]:
        dbs = [self.database] if self.database else self._databases()
        out = []
        with self._lock, self.conn.cursor() as cur:
            for db in dbs:
                if db in SYSTEM_DBS:
                    continue
                cur.execute(
                    "SELECT c.TABLE_NAME, c.COLUMN_NAME, c.COLUMN_TYPE, c.COLUMN_COMMENT, t.TABLE_COMMENT "
                    "FROM information_schema.columns c "
                    "JOIN information_schema.tables t "
                    "  ON t.TABLE_SCHEMA = c.TABLE_SCHEMA AND t.TABLE_NAME = c.TABLE_NAME "
                    "WHERE c.TABLE_SCHEMA = %s ORDER BY c.TABLE_NAME, c.ORDINAL_POSITION", [db])
                for table, col, typ, ccomment, tcomment in cur.fetchall():
                    out.append(Column(self._fqn(db, table), col, typ, ccomment or None, tcomment or None))
        return out

    def table_sizes(self) -> dict[str, int]:
        dbs = [self.database] if self.database else self._databases()
        out = {}
        with self._lock, self.conn.cursor() as cur:
            for db in dbs:
                if db in SYSTEM_DBS:
                    continue
                cur.execute("SELECT TABLE_NAME, TABLE_ROWS FROM information_schema.tables "
                           "WHERE TABLE_SCHEMA = %s", [db])
                for table, rows in cur.fetchall():
                    if rows is not None:
                        out[self._fqn(db, table)] = int(rows)
        return out

    def diagnose(self) -> dict:
        report: dict = {"sources": [{"name": self.database or "mysql", "kind": "mysql"}]}
        try:
            with self._lock, self.conn.cursor() as cur:
                cur.execute("SHOW DATABASES")
                report["databases"] = [r[0] for r in cur.fetchall()]
                cur.execute("SELECT TABLE_SCHEMA, TABLE_NAME FROM information_schema.tables "
                           "WHERE TABLE_SCHEMA NOT IN %s LIMIT 200", [tuple(SYSTEM_DBS)])
                report["tables"] = [tuple(r) for r in cur.fetchall()]
        except Exception as e:
            report["error"] = str(e).splitlines()[0][:200]
        return report

    # ------------------------------------------------------------ execution
    def execute(self, sql: str, timeout_s: float | None = None) -> pa.Table:
        with self._lock:
            with self.conn.cursor() as cur:
                if timeout_s:
                    try:
                        cur.execute(f"SET SESSION MAX_EXECUTION_TIME={int(timeout_s * 1000)}")
                    except Exception:
                        pass          # not every MySQL build supports it; run unbounded rather than fail
                try:
                    cur.execute(sql)
                except Exception as e:
                    raise EngineError(str(e).splitlines()[0][:300]) from e
                cols = [d[0] for d in (cur.description or [])]
                rows = cur.fetchall() if cur.description is not None else []
        arrays = [pa.array([r[i] for r in rows]) for i in range(len(cols))] if cols else []
        return pa.Table.from_arrays(arrays, names=cols) if cols else pa.table({})
