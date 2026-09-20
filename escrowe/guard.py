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
READ_STATEMENTS = (exp.Select, exp.Union)

# Only kinds sqlglot actually knows get their own dialect (so the parser
# tolerates that database's own syntax); anything else parses generically,
# which is plenty for the one thing checked here: statement shape.
_SQLGLOT_DIALECTS = {"mysql", "postgres", "duckdb", "sqlite", "snowflake", "bigquery"}

# A file write disguised as a SELECT. sqlglot's parsers already fail on this
# syntax today (so it would be denied anyway, as an unparseable statement),
# but that's a side effect of what the parser happens to support, not a rule
# - this makes it one, so it stays denied even if a future sqlglot release
# adds support for the syntax.
_FILE_WRITE = re.compile(r"\binto\s+(outfile|dumpfile)\b", re.I)

# Built-ins with no legitimate "read a table" purpose: they stall the
# connection, manipulate server-wide locks, or read a file off the server's
# own disk rather than a table's data.
BLOCKED_FUNCTIONS = {
    "sleep", "benchmark", "get_lock", "release_lock", "release_all_locks",
    "is_free_lock", "is_used_lock", "master_pos_wait", "source_pos_wait",
    "gtid_wait", "wait_for_executed_gtid_set", "load_file",
}


def _fn_name(node: exp.Expression) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.this).lower()
    return type(node).__name__.lower()


def check(sql: str, allow_write: bool, dialect: str | None = None) -> str:
    """Returns the SQL to run (the original text, unchanged), or raises Denied."""
    if _FILE_WRITE.search(sql):
        raise Denied("SELECT ... INTO OUTFILE/DUMPFILE is not something escrowe runs: "
                     "it writes a file on the database server, not a read.")
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
