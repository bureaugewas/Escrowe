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
