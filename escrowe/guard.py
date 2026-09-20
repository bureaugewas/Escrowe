"""The only checks escrowe itself makes on a query: is it one statement, is
it read-only for the agent, and does it try to slip a write or a file/server
operation in through something that looks like a SELECT. Everything else -
which tables, which rows, which columns, and any write a *person* runs
themselves - is entirely up to the database account escrowe is connected as.
There is no rewrite: what you or the agent write is what runs.

Two things below are hard denials for everyone, not just the agent, because
they aren't really reads even though they parse as one:

  SELECT ... INTO OUTFILE/DUMPFILE   writes a file to the database SERVER's
                                     own filesystem - a write wearing a
                                     SELECT's clothes.
  A stored procedure CALL, or a blocked built-in like SLEEP()/GET_LOCK()/
  LOAD_FILE()/BENCHMARK()            a stored procedure can do absolutely
                                     anything its own definition allows,
                                     including write - escrowe has no way to
                                     know from the call site, so it's not
                                     something escrow runs. The blocked
                                     built-ins are ones with no legitimate
                                     read-a-table use: they stall the
                                     connection, hold/release server-wide
                                     locks, or read arbitrary server files.
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
# EXCEPT/INTERSECT are exactly as read-only as UNION - two SELECTs combined,
# no side effects - they were just never added alongside it.
READ_STATEMENTS = (exp.Select, exp.Union, exp.Except, exp.Intersect)

# Only kinds sqlglot actually knows get their own dialect (so the parser
# tolerates that database's own syntax); anything else parses generically,
# which is plenty for the one thing checked here: statement shape.
_SQLGLOT_DIALECTS = {"mysql", "postgres", "duckdb", "sqlite", "snowflake", "bigquery", "tsql"}
# escrowe's own engine `kind` names don't always match sqlglot's dialect names:
# sqlserver's T-SQL dialect is called "tsql" there, and a DuckLake catalog is a
# DuckDB catalog format - same SQL dialect, just a different kind of catalog.
# Without this, e.g. `SELECT TOP 5 ...` / `[bracketed]` identifiers on SQL
# Server parsed generically and were denied as unparseable, not because T-SQL
# genuinely isn't supported.
_DIALECT_ALIASES = {"sqlserver": "tsql", "ducklake": "duckdb"}

# A file write disguised as a SELECT. sqlglot's parsers already fail on this
# syntax today (so it would be denied anyway, as an unparseable statement),
# but that's a side effect of what the parser happens to support, not a rule
# - this makes it one, so it stays denied even if a future sqlglot release
# adds support for the syntax.
_FILE_WRITE = re.compile(r"\binto\s+(outfile|dumpfile)\b", re.I)

# Built-ins with no legitimate "read a table" purpose: they stall the
# connection, manipulate server-wide locks, or read a file/the environment
# off the server's own disk rather than a table's data - the same reasoning
# across every engine, just different names. Only MySQL's own set (sleep,
# locks, load_file) was covered until this list was extended to match: a
# database-account grant is a natural mental model for "which tables", not
# "can this read arbitrary files off the host" - so this stays a hard denial
# for everyone (agent and person alike), same as the MySQL functions already
# were, rather than something left to the connected account's own privileges.
BLOCKED_FUNCTIONS = {
    # MySQL/MariaDB
    "sleep", "benchmark", "get_lock", "release_lock", "release_all_locks",
    "is_free_lock", "is_used_lock", "master_pos_wait", "source_pos_wait",
    "gtid_wait", "wait_for_executed_gtid_set", "load_file",
    # PostgreSQL - file/env reads and connection/session manipulation
    "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file",
    "pg_sleep", "pg_sleep_for", "pg_sleep_until", "pg_terminate_backend",
    "pg_cancel_backend", "lo_import", "lo_export",
    # DuckDB - reads a server file/env var rather than an attached table.
    # read_parquet/read_csv also legitimately reads a lake's own data files
    # via DuckLake, but the agent's SQL never needs to call it directly -
    # DuckLake tables are already attached and queryable by plain name.
    "getenv", "read_text", "read_blob", "read_csv", "read_csv_auto",
    "read_json", "read_json_auto", "read_parquet", "glob",
    # SQLite - readfile/writefile move file contents in/out through a
    # function call; load_extension loads a native shared library, i.e.
    # arbitrary code execution, not just a data-boundary question.
    "readfile", "writefile", "load_extension",
}


_CALL_NAME = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def _fn_name(node: exp.Expression) -> str:
    if isinstance(node, exp.Anonymous):
        # .name (not str(node.this)) so a quoted call - `SLEEP`(5) - matches
        # the blocklist instead of comparing against '"sleep"' with the
        # quote characters baked in.
        this = node.this
        return this.name.lower() if isinstance(this, exp.Expression) else str(this).lower()
    # A function sqlglot knows by its own node type (e.g. exp.ReadCSV for
    # READ_CSV) rather than exp.Anonymous - the class name doesn't reliably
    # preserve the original underscored spelling (ReadCSV, not Read_Csv), so
    # take the name from the rendered SQL itself instead of guessing at
    # sqlglot's internal naming convention.
    m = _CALL_NAME.match(node.sql())
    return m.group(1).lower() if m else type(node).__name__.lower()


def check(sql: str, allow_write: bool, dialect: str | None = None) -> str:
    """Returns the SQL to run (the original text, unchanged), or raises Denied."""
    if _FILE_WRITE.search(sql):
        raise Denied("SELECT ... INTO OUTFILE/DUMPFILE is not something escrowe runs: "
                     "it writes a file on the database server, not a read.")
    dialect = _DIALECT_ALIASES.get(dialect, dialect)
    read = dialect if dialect in _SQLGLOT_DIALECTS else None
    try:
        statements = [s for s in sqlglot.parse(sql, read=read) if s is not None]
    except ParseError as e:
        raise Denied(f"SQL could not be parsed: {e}")
    if len(statements) != 1:
        raise Denied("Exactly one statement is allowed per request.")
    stmt = statements[0]
    if isinstance(stmt, WRITE_STATEMENTS) and not allow_write:
        raise Denied("The agent may only read.")
    # SELECT ... INTO <table> (SQL Server/Sybase) parses as an ordinary
    # exp.Select - sqlglot has no separate node for it - but it creates a
    # table, a write wearing a SELECT's clothes exactly like INTO OUTFILE
    # above. Without this it would sail through the read-only check.
    if isinstance(stmt, exp.Select) and stmt.args.get("into") and not allow_write:
        raise Denied("The agent may only read.")
    if not isinstance(stmt, READ_STATEMENTS + WRITE_STATEMENTS):
        if re.match(r"^\s*call\b", sql, re.I):
            raise Denied("CALL (a stored procedure) is not something escrowe runs: its "
                         "definition could do anything, including write, with no way for "
                         "escrowe to tell from the call site.")
        raise Denied(f"{type(stmt).__name__.upper()} is not something escrowe runs.")
    for f in stmt.find_all(exp.Func):
        name = _fn_name(f)
        if name in BLOCKED_FUNCTIONS:
            raise Denied(f"{name.upper()}() is not something escrowe runs.")
    return sql.strip().rstrip(";")
