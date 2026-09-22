"""MySQL / MariaDB through PyMySQL."""

from __future__ import annotations

from .base import Column, DBAPIEngine, EngineError

SYSTEM_DBS = ("information_schema", "mysql", "performance_schema", "sys")


class MySQLEngine(DBAPIEngine):
    kind = "mysql"
    default_port = 3306

    def __init__(self, host: str, user: str, password: str, port: int = 3306,
                 database: str | None = None, connect_timeout: float = 10.0, **_):
        super().__init__()
        try:
            import pymysql
        except ImportError as e:
            raise EngineError("MySQL support needs the 'pymysql' package.") from e
        self.database = database
        self._connect(pymysql.connect, host=host, user=user, password=password, port=int(port),
                      database=database, connect_timeout=connect_timeout)

    def _databases(self) -> list[str]:
        if self.database:
            return [self.database]
        return [r[0] for r in self._fetchall("SHOW DATABASES") if r[0] not in SYSTEM_DBS]

    def _fqn(self, db: str, table: str) -> str:
        # Connected to one database: the bare table name resolves. Connected to
        # a whole server: qualify with the database name.
        return table.lower() if self.database else f"{db}.{table}".lower()

    def catalog(self) -> list[Column]:
        out = []
        for db in self._databases():
            rows = self._fetchall(
                "SELECT c.TABLE_NAME, c.COLUMN_NAME, c.COLUMN_TYPE, c.COLUMN_COMMENT, t.TABLE_COMMENT "
                "FROM information_schema.columns c "
                "JOIN information_schema.tables t "
                "  ON t.TABLE_SCHEMA = c.TABLE_SCHEMA AND t.TABLE_NAME = c.TABLE_NAME "
                "WHERE c.TABLE_SCHEMA = %s ORDER BY c.TABLE_NAME, c.ORDINAL_POSITION", [db])
            out += [Column(self._fqn(db, table), col, typ, ccomment or None, tcomment or None)
                    for table, col, typ, ccomment, tcomment in rows]
        return out

    def table_sizes(self) -> dict[str, int]:
        out = {}
        for db in self._databases():
            rows = self._fetchall("SELECT TABLE_NAME, TABLE_ROWS FROM information_schema.tables "
                                  "WHERE TABLE_SCHEMA = %s", [db])
            out.update({self._fqn(db, t): int(n) for t, n in rows if n is not None})
        return out

    def _apply_timeout(self, cur, timeout_s: float) -> None:
        cur.execute(f"SET SESSION MAX_EXECUTION_TIME={int(timeout_s * 1000)}")
