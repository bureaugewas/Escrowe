"""The only checks escrowe makes on a query before running it.

For everyone (person or agent):
  - exactly one statement
  - no `SELECT ... INTO OUTFILE/DUMPFILE` (writes a file on the database server)
  - no stored-procedure CALL (its body could do anything)
  - no built-in that stalls the connection, takes server-wide locks, or reads
    files or the environment off the server's own disk (see BLOCKED_FUNCTIONS)

For the agent only:
  - the statement must be a read (SELECT / UNION / EXCEPT / INTERSECT)

Which tables, rows and columns are reachable is not decided here: that is
the connected database account's own grants. Nothing is rewritten; the SQL
that passes is the SQL that runs.
"""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError


class Denied(Exception):
    pass


WRITE_STATEMENTS = (exp.Insert, exp.Update, exp.Delete, exp.Merge,
                    exp.Create, exp.Drop, exp.Alter, exp.TruncateTable)
READ_STATEMENTS = (exp.Select, exp.Union, exp.Except, exp.Intersect)

# Kinds sqlglot has a dialect for parse with it; anything else parses
# generically, which suffices for the one thing checked here: statement shape.
_SQLGLOT_DIALECTS = {"mysql", "postgres", "duckdb", "sqlite", "snowflake", "bigquery", "tsql"}
_DIALECT_ALIASES = {"sqlserver": "tsql", "ducklake": "duckdb"}

# Checked on the raw text, so it stays denied even if a future sqlglot
# release learns to parse the syntax.
_FILE_WRITE = re.compile(r"\binto\s+(outfile|dumpfile)\b", re.I)
_CALL = re.compile(r"^\s*call\b", re.I)
_CALL_NAME = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(")

BLOCKED_FUNCTIONS = {
    # MySQL / MariaDB
    "sleep", "benchmark", "get_lock", "release_lock", "release_all_locks",
    "is_free_lock", "is_used_lock", "master_pos_wait", "source_pos_wait",
    "gtid_wait", "wait_for_executed_gtid_set", "load_file",
    # PostgreSQL
    "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file",
    "pg_sleep", "pg_sleep_for", "pg_sleep_until", "pg_terminate_backend",
    "pg_cancel_backend", "lo_import", "lo_export",
    # DuckDB: reads a server file or env var. DuckLake tables are already
    # attached and queryable by name, so the agent never needs these.
    "getenv", "read_text", "read_blob", "read_csv", "read_csv_auto",
    "read_json", "read_json_auto", "read_parquet", "glob",
    # SQLite: file I/O through a function call; load_extension runs native code.
    "readfile", "writefile", "load_extension",
}


def _function_name(node: exp.Expression) -> str:
    if isinstance(node, exp.Anonymous):
        this = node.this
        # `.name` so a quoted call like `SLEEP`(5) matches without the quotes
        return this.name.lower() if isinstance(this, exp.Expression) else str(this).lower()
    # A function sqlglot knows by node type (exp.ReadCSV for READ_CSV): take
    # the name from the rendered SQL rather than guessing from the class name.
    m = _CALL_NAME.match(node.sql())
    return m.group(1).lower() if m else type(node).__name__.lower()


def check(sql: str, allow_write: bool, dialect: str | None = None) -> str:
    """Return the SQL to run (the original text, trimmed), or raise Denied."""
    if _FILE_WRITE.search(sql):
        raise Denied("SELECT ... INTO OUTFILE/DUMPFILE writes a file on the database server; "
                     "escrowe does not run it.")
    if _CALL.match(sql):
        raise Denied("CALL (a stored procedure) could do anything, including write; "
                     "escrowe does not run it.")
    dialect = _DIALECT_ALIASES.get(dialect, dialect)
    read = dialect if dialect in _SQLGLOT_DIALECTS else None
    try:
        statements = [s for s in sqlglot.parse(sql, read=read) if s is not None]
    except ParseError as e:
        raise Denied(f"SQL could not be parsed: {e}")
    if len(statements) != 1:
        raise Denied("Exactly one statement is allowed per request.")
    stmt = statements[0]

    if not allow_write:
        if isinstance(stmt, WRITE_STATEMENTS):
            raise Denied("The agent may only read.")
        # SELECT ... INTO <table> (SQL Server) parses as a plain SELECT but creates a table.
        if isinstance(stmt, exp.Select) and stmt.args.get("into"):
            raise Denied("The agent may only read.")
    if not isinstance(stmt, READ_STATEMENTS + WRITE_STATEMENTS):
        raise Denied(f"{type(stmt).__name__.upper()} is not something escrowe runs.")
    for func in stmt.find_all(exp.Func):
        name = _function_name(func)
        if name in BLOCKED_FUNCTIONS:
            raise Denied(f"{name.upper()}() is not something escrowe runs.")
    return sql.strip().rstrip(";")
