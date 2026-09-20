"""PostgreSQL, connected directly: a real psycopg connection, opened with
whatever account the person configured, nothing in between. The account's own
GRANTs are the only access control; escrowe does not add or remove any."""

from __future__ import annotations

import threading

import pyarrow as pa

from .base import Column, DirectEngine, EngineError

SYSTEM_SCHEMAS = {"pg_catalog", "information_schema", "pg_toast"}


class PostgresEngine(DirectEngine):
    kind = "postgres"
    default_port = 5432

    def __init__(self, host: str, user: str, password: str, port: int = 5432,
                 database: str | None = None, connect_timeout: float = 10.0):
        try:
            import psycopg
        except ImportError as e:
            raise EngineError("Postgres support needs the 'psycopg[binary]' package (pip install psycopg[binary]).") from e
        self._lock = threading.Lock()
        self.database = database
        try:
            self.conn = psycopg.connect(host=host, user=user, password=password, port=int(port),
                                        dbname=database or "postgres", connect_timeout=connect_timeout,
                                        autocommit=True)
        except Exception as e:
            raise EngineError(str(e).splitlines()[0][:300]) from e

    @classmethod
    def test_login(cls, host: str, user: str, password: str, port: int = 5432,
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
            cur.execute(
                "SELECT c.table_name, c.column_name, c.data_type, "
                "  col_description(format('%%s.%%s', c.table_schema, c.table_name)::regclass::oid, c.ordinal_position), "
                "  obj_description(format('%%s.%%s', c.table_schema, c.table_name)::regclass::oid) "
                "FROM information_schema.columns c "
                "JOIN information_schema.tables t "
                "  ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
                "WHERE t.table_schema != ALL(%s) AND t.table_type = 'BASE TABLE' "
                "ORDER BY c.table_name, c.ordinal_position",
                [list(SYSTEM_SCHEMAS)])
            rows = cur.fetchall()
        return [Column(table.lower(), col, typ, ccomment or None, tcomment or None)
                for table, col, typ, ccomment, tcomment in rows]

    def table_sizes(self) -> dict[str, int]:
        with self._lock, self.conn.cursor() as cur:
            cur.execute(
                "SELECT relname, reltuples FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE c.relkind = 'r' AND n.nspname != ALL(%s)", [list(SYSTEM_SCHEMAS)])
            return {name.lower(): int(est) for name, est in cur.fetchall() if est is not None and est >= 0}

    def diagnose(self) -> dict:
        report: dict = {"sources": [{"name": self.database or "postgres", "kind": "postgres"}]}
        try:
            with self._lock, self.conn.cursor() as cur:
                cur.execute("SELECT table_schema, table_name FROM information_schema.tables "
                           "WHERE table_schema != ALL(%s) LIMIT 200", [list(SYSTEM_SCHEMAS)])
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
                        cur.execute(f"SET statement_timeout = {int(timeout_s * 1000)}")
                    except Exception:
                        pass
                try:
                    cur.execute(sql)
                except Exception as e:
                    raise EngineError(str(e).splitlines()[0][:300]) from e
                cols = [d[0] for d in (cur.description or [])]
                rows = cur.fetchall() if cur.description is not None else []
        arrays = [pa.array([r[i] for r in rows]) for i in range(len(cols))] if cols else []
        return pa.Table.from_arrays(arrays, names=cols) if cols else pa.table({})
