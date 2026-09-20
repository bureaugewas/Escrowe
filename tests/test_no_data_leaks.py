"""Deeper leak-guarantee tests than test_blind.py: values of every type
(numbers, dates, strings, NULLs, odd column names) must never reach the
agent; per-session history must never carry rows; the exact-repeat replay in
Escrowe.ask() must never re-invoke the agent and must be scoped per session;
probe/_shape_feedback must never carry a value, even a unique-looking one;
and the transcript log must hold to the same guarantee."""

from __future__ import annotations

import pytest
import duckdb

from escrowe.agent import Agent
from escrowe.config import Attachment, Settings
from escrowe.engines import REGISTRY
from escrowe.engines.base import Column, DirectEngine, EngineError
from escrowe.service import Escrowe, _shape_feedback
from escrowe.store import Store

from test_blind import RecordingAgent

# Values of every ordinary type, plus a NULL and a column name with spaces -
# none of these strings/numbers should ever be findable in a prompt.
LEAKY_VALUES = ["UNIQUE_SECRET_STRING_42", "another-value!!", "3.14159", "-7.0",
               "2024-01-01", "1999-12-31", "not null here"]


class WeirdEngine(DirectEngine):
    """A tiny engine with deliberately odd data: floats, dates, NULLs and a
    column name with spaces - broader coverage than fake_engine.py's plain
    tables, on a dedicated kind so the shared fixtures stay untouched."""
    kind = "weird"
    default_port = 0
    requires_credentials = False

    def __init__(self, **_):
        self.conn = duckdb.connect(":memory:")
        self.conn.execute(
            'CREATE TABLE secrets ("weird col name" INTEGER, val VARCHAR, amount DOUBLE, '
            'dt DATE, "n u l l a b l e" VARCHAR)')
        self.conn.execute(
            "INSERT INTO secrets VALUES "
            "(1, 'UNIQUE_SECRET_STRING_42', 3.14159, DATE '2024-01-01', NULL), "
            "(2, 'another-value!!', -7.0, DATE '1999-12-31', 'not null here')")

    @classmethod
    def test_login(cls, **params) -> None:
        cls(**params).close()

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    def catalog(self):
        rows = self.conn.execute(
            "SELECT column_name, data_type FROM duckdb_columns() "
            "WHERE table_name = 'secrets' ORDER BY column_index").fetchall()
        return [Column("secrets", name, typ, None, None) for name, typ in rows]

    def table_sizes(self) -> dict[str, int]:
        return {"secrets": 2}

    def execute(self, sql: str, timeout_s: float | None = None):
        try:
            return self.conn.execute(sql).to_arrow_table()
        except Exception as e:
            raise EngineError(str(e).splitlines()[0][:300]) from e


REGISTRY.setdefault("weird", WeirdEngine)


@pytest.fixture
def wsvc(tmp_path):
    settings = Settings(home=tmp_path, jwt_secret="test-secret",
                        attachments=[Attachment("w", "weird", "x=1")], llm_provider="mock")
    return Escrowe(settings, store=Store(tmp_path / "cat.sqlite"), agent=Agent("mock"))


@pytest.fixture
def op(wsvc):
    return wsvc.operator_principal()


def test_no_row_value_of_any_type_reaches_the_agent(wsvc, op):
    agent = RecordingAgent(['{"sql": "SELECT * FROM secrets"}'])
    wsvc.agent = agent
    res = wsvc.ask(op, "show me everything")
    assert res.table.num_rows == 2
    blob = "\n".join(agent.seen)
    for v in LEAKY_VALUES:
        assert v not in blob
    # metadata (names, types) is exactly what's supposed to be there
    assert "weird col name" in blob and "secrets" in blob


def test_history_turn_carries_shape_never_rows(wsvc, op):
    wsvc.agent = RecordingAgent(['{"sql": "SELECT * FROM secrets"}'])
    wsvc.ask(op, "show me everything")
    sid = op.session or wsvc._operator_session_id
    turn = wsvc._history[sid][-1]
    assert turn.fed is None
    assert turn.shape and "2 row(s) returned" in turn.shape
    for v in LEAKY_VALUES:
        assert v not in (turn.shape or "")


def test_feed_data_does_not_leak_into_a_different_session(wsvc):
    p1 = wsvc.principal(wsvc.login("u1", "x")["token"])
    p2 = wsvc.principal(wsvc.login("u2", "x")["token"])
    assert p1.session != p2.session

    wsvc.agent = RecordingAgent(["an answer about the fed data"])
    wsvc.ask(p1, "what does this show?", feed_data="SECRET_ROW_VALUE_XYZ")
    assert wsvc._history[p1.session][-1].fed == "SECRET_ROW_VALUE_XYZ"

    # p2's history is untouched, and asking something in p2's session must
    # never render p1's fed data into the prompt.
    assert p2.session not in wsvc._history or wsvc._history[p2.session] == []
    agent2 = RecordingAgent(['{"sql": "SELECT * FROM secrets"}'])
    wsvc.agent = agent2
    wsvc.ask(p2, "show me everything")
    blob = "\n".join(agent2.seen)
    assert "SECRET_ROW_VALUE_XYZ" not in blob


def test_exact_repeat_question_never_reinvokes_the_agent(wsvc, op):
    wsvc.agent = RecordingAgent(['{"sql": "SELECT count(*) AS n FROM secrets"}'])
    r1 = wsvc.ask(op, "how many rows?")
    seen_after_first = len(wsvc.agent.seen)
    r2 = wsvc.ask(op, "how many rows?")
    assert len(wsvc.agent.seen) == seen_after_first        # no new prompt sent
    assert r2 is r1


def test_exact_repeat_replay_is_scoped_per_session(wsvc):
    p1 = wsvc.principal(wsvc.login("u1", "x")["token"])
    p2 = wsvc.principal(wsvc.login("u2", "x")["token"])
    wsvc.agent = RecordingAgent(['{"sql": "SELECT count(*) AS n FROM secrets"}',
                                 '{"sql": "SELECT count(*) AS n FROM secrets"}'])
    wsvc.ask(p1, "how many rows?")
    wsvc.ask(p2, "how many rows?")     # a different session: must call the agent again
    assert len(wsvc.agent.seen) == 2


def test_probe_shape_feedback_never_reveals_a_value_even_when_unique():
    import pyarrow as pa
    table = pa.table({"val": ["UNIQUE_SECRET_STRING_42", None]})
    fb = _shape_feedback(table)
    assert "UNIQUE_SECRET_STRING_42" not in fb
    assert "val: 1/2 null" in fb


def test_probe_shape_feedback_on_empty_result():
    import pyarrow as pa
    table = pa.table({"val": pa.array([], type=pa.string())})
    assert _shape_feedback(table) == "0 rows returned."


def test_transcript_never_logs_a_row_value(wsvc, op):
    wsvc.agent = RecordingAgent(['{"sql": "SELECT * FROM secrets"}'])
    wsvc.agent.transcript = wsvc.transcript
    wsvc.ask(op, "show me everything")

    from escrowe.transcript import Transcript, default_path
    entries = Transcript(default_path(wsvc.settings.home)).read()
    blob = "\n".join(e["sent"]["system"] + e["sent"]["user"] + (e["received"] or "") for e in entries)
    for v in LEAKY_VALUES:
        assert v not in blob
