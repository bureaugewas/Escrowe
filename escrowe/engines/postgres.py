"""PostgreSQL through psycopg."""

from __future__ import annotations

from .base import Column, DBAPIEngine, EngineError

SYSTEM_SCHEMAS = ["pg_catalog", "information_schema", "pg_toast"]


class PostgresEngine(DBAPIEngine):
    kind = "postgres"
    default_port = 5432

    def __init__(self, host: str, user: str, password: str, port: int = 5432,
                 database: str | None = None, connect_timeout: float = 10.0, **_):
        super().__init__()
        try:
            import psycopg
        except ImportError as e:
            raise EngineError("Postgres support needs the 'psycopg[binary]' package.") from e
        self.database = database
        self._connect(psycopg.connect, host=host, user=user, password=password, port=int(port),
                      dbname=database or "postgres", connect_timeout=connect_timeout, autocommit=True)

    def catalog(self) -> list[Column]:
        rows = self._fetchall(
            "SELECT c.table_name, c.column_name, c.data_type, "
            "  col_description(format('%%s.%%s', c.table_schema, c.table_name)::regclass::oid, c.ordinal_position), "
            "  obj_description(format('%%s.%%s', c.table_schema, c.table_name)::regclass::oid) "
            "FROM information_schema.columns c "
            "JOIN information_schema.tables t "
            "  ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
            "WHERE t.table_schema != ALL(%s) AND t.table_type = 'BASE TABLE' "
            "ORDER BY c.table_name, c.ordinal_position", [SYSTEM_SCHEMAS])
        return [Column(table.lower(), col, typ, ccomment or None, tcomment or None)
                for table, col, typ, ccomment, tcomment in rows]

    def table_sizes(self) -> dict[str, int]:
        rows = self._fetchall(
            "SELECT relname, reltuples FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relkind = 'r' AND n.nspname != ALL(%s)", [SYSTEM_SCHEMAS])
        return {name.lower(): int(est) for name, est in rows if est is not None and est >= 0}

    def _apply_timeout(self, cur, timeout_s: float) -> None:
        cur.execute(f"SET statement_timeout = {int(timeout_s * 1000)}")
