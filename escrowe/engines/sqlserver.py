"""SQL Server through pymssql (a pure TDS client, no ODBC driver needed)."""

from __future__ import annotations

from .base import Column, DBAPIEngine, EngineError

SYSTEM_SCHEMAS = ("sys", "INFORMATION_SCHEMA", "guest", "db_owner", "db_accessadmin",
                  "db_securityadmin", "db_ddladmin", "db_backupoperator", "db_datareader",
                  "db_datawriter", "db_denydatareader", "db_denydatawriter")


class SQLServerEngine(DBAPIEngine):
    kind = "sqlserver"
    default_port = 1433

    def __init__(self, host: str, user: str, password: str, port: int = 1433,
                 database: str | None = None, connect_timeout: float = 10.0, **_):
        super().__init__()
        try:
            import pymssql
        except ImportError as e:
            raise EngineError("SQL Server support needs the 'pymssql' package.") from e
        self.database = database
        self._connect(pymssql.connect, server=host, user=user, password=password, port=int(port),
                      database=database or "", login_timeout=int(connect_timeout), autocommit=True)

    def catalog(self) -> list[Column]:
        # No comments: SQL Server keeps them in extended_properties, which
        # needs a per-column join that is not worth it. They stay None.
        placeholders = ",".join("%s" for _ in SYSTEM_SCHEMAS)
        rows = self._fetchall(
            "SELECT c.TABLE_NAME, c.COLUMN_NAME, c.DATA_TYPE "
            "FROM INFORMATION_SCHEMA.COLUMNS c "
            "JOIN INFORMATION_SCHEMA.TABLES t "
            "  ON t.TABLE_SCHEMA = c.TABLE_SCHEMA AND t.TABLE_NAME = c.TABLE_NAME "
            f"WHERE t.TABLE_TYPE = 'BASE TABLE' AND t.TABLE_SCHEMA NOT IN ({placeholders}) "
            "ORDER BY c.TABLE_NAME, c.ORDINAL_POSITION", SYSTEM_SCHEMAS)
        return [Column(table.lower(), col, typ) for table, col, typ in rows]

    def table_sizes(self) -> dict[str, int]:
        rows = self._fetchall(
            "SELECT t.name, SUM(p.rows) FROM sys.tables t "
            "JOIN sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0, 1) "
            "GROUP BY t.name")
        return {name.lower(): int(n) for name, n in rows if n is not None}

    def _apply_timeout(self, cur, timeout_s: float) -> None:
        self.conn._conn.query_timeout = int(timeout_s)
