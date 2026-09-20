"""The hard denials in guard.py: things that parse as a SELECT but aren't
really a read, and stored procedure calls whose behavior escrowe can't see
from the call site. These apply regardless of allow_write - a person running
SQL themselves is blocked by them too, not just the agent."""

import pytest

from escrowe.guard import Denied, check


@pytest.mark.parametrize("sql", [
    "SELECT * FROM t INTO OUTFILE '/tmp/x.csv'",
    "SELECT a, b INTO OUTFILE '/tmp/x' FROM t",
    "select * from t into dumpfile '/tmp/x'",
])
def test_select_into_outfile_is_denied(sql):
    with pytest.raises(Denied, match="OUTFILE|DUMPFILE"):
        check(sql, allow_write=True, dialect="mysql")


def test_call_is_denied_even_for_a_person():
    with pytest.raises(Denied, match="stored procedure"):
        check("CALL my_proc(1, 2)", allow_write=True, dialect="mysql")


@pytest.mark.parametrize("sql", [
    "SELECT SLEEP(5)",
    "SELECT BENCHMARK(1000000, MD5('x'))",
    "SELECT GET_LOCK('x', 1)",
    "SELECT RELEASE_LOCK('x')",
    "SELECT LOAD_FILE('/etc/passwd')",
])
def test_dangerous_builtins_are_denied(sql):
    with pytest.raises(Denied):
        check(sql, allow_write=True, dialect="mysql")


def test_ordinary_functions_still_work():
    assert check("SELECT COUNT(*) FROM customers", allow_write=False, dialect="mysql")
    assert check("SELECT name, UPPER(name) FROM customers", allow_write=False, dialect="mysql")


def test_write_statements_still_blocked_for_the_agent_only():
    with pytest.raises(Denied, match="only read"):
        check("DELETE FROM customers", allow_write=False, dialect="mysql")
    assert check("DELETE FROM customers", allow_write=True, dialect="mysql")


def test_sqlserver_dialect_alias_lets_real_tsql_syntax_parse():
    """escrowe's engine kind is "sqlserver"; sqlglot's own dialect name for
    T-SQL is "tsql". Passing the kind straight through used to fall back to
    generic parsing, denying ordinary T-SQL as unparseable rather than because
    it's genuinely unsupported."""
    assert check("SELECT TOP 5 * FROM customers", allow_write=False, dialect="sqlserver")
    assert check("SELECT * FROM [customers]", allow_write=False, dialect="sqlserver")
    # the write-guard must still hold once the dialect actually parses this
    with pytest.raises(Denied, match="only read"):
        check("SELECT TOP 5 * INTO newtable FROM customers", allow_write=False, dialect="sqlserver")


def test_ducklake_dialect_alias_parses_as_duckdb():
    assert check("SELECT * FROM customers", allow_write=False, dialect="ducklake")


@pytest.mark.parametrize("sql,dialect", [
    ("SELECT `SLEEP`(5)", "mysql"),                        # backtick-quoted: name mustn't include the quotes
    ("SELECT read_csv('/etc/passwd')", "duckdb"),
    ("SELECT read_csv_auto('/etc/passwd')", "duckdb"),
    ("SELECT read_json('/etc/passwd')", "duckdb"),
    ("SELECT read_parquet('/etc/passwd')", "duckdb"),
    ("SELECT read_text('/etc/passwd')", "duckdb"),
    ("SELECT getenv('HOME')", "duckdb"),
    ("SELECT * FROM glob('/etc/*')", "duckdb"),
    ("SELECT readfile('/etc/passwd')", "sqlite"),
    ("SELECT writefile('/tmp/x', 'y')", "sqlite"),
    ("SELECT load_extension('/tmp/x.so')", "sqlite"),
    ("SELECT pg_read_file('/etc/passwd')", "postgres"),
    ("SELECT pg_read_binary_file('/etc/passwd')", "postgres"),
    ("SELECT pg_ls_dir('/etc')", "postgres"),
    ("SELECT pg_sleep(10)", "postgres"),
    ("SELECT pg_terminate_backend(123)", "postgres"),
    ("SELECT pg_cancel_backend(123)", "postgres"),
])
def test_server_side_file_and_env_reads_are_denied_across_engines(sql, dialect):
    """These parse as an ordinary SELECT but read the server's own filesystem
    or environment rather than a table - the same class of problem as
    MySQL's LOAD_FILE, just under a different name per engine."""
    with pytest.raises(Denied):
        check(sql, allow_write=True, dialect=dialect)


@pytest.mark.parametrize("sql,dialect", [
    ("SELECT id FROM a EXCEPT SELECT id FROM b", "postgres"),
    ("SELECT id FROM a EXCEPT SELECT id FROM b", "sqlserver"),
    ("SELECT id FROM a INTERSECT SELECT id FROM b", "postgres"),
])
def test_except_and_intersect_are_allowed_reads(sql, dialect):
    """As read-only as UNION (already allowed) - two SELECTs combined, no
    side effects. There was no security reason these were denied; they were
    just missing from READ_STATEMENTS."""
    assert check(sql, allow_write=False, dialect=dialect)
