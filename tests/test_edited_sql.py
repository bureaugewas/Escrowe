"""A question's SQL edited by hand becomes that question's latest state: the
next prompt shows it (marked as the person's edit), a repeat no longer
replays the agent's version, and clearing the context forgets all of it."""

from __future__ import annotations

from test_blind import RecordingAgent

AGENT_SQL = "SELECT tier, count(*) AS n FROM customers GROUP BY tier"
EDITED_SQL = "SELECT tier FROM customers WHERE tier IS NOT NULL GROUP BY tier"


def test_edited_sql_runs_and_becomes_the_context(svc, alice):
    svc.agent = agent = RecordingAgent([f'{{"sql": "{AGENT_SQL}"}}', '{"sql": "SELECT 1 AS x"}'])
    svc.ask(alice, "customers per tier")
    res = svc.run_edited(alice, "customers per tier", EDITED_SQL)
    assert res.sql == EDITED_SQL and res.table.column_names == ["tier"]

    svc.ask(alice, "customers per tier, only gold")
    prompt = agent.seen[-1]
    assert f"SQL (edited by the person): {EDITED_SQL}" in prompt
    assert AGENT_SQL not in prompt              # the superseded version is gone


def test_rerunning_unchanged_sql_is_not_called_an_edit(svc, alice):
    svc.agent = RecordingAgent([f'{{"sql": "{AGENT_SQL}"}}'])
    svc.ask(alice, "customers per tier")
    svc.run_edited(alice, "customers per tier", AGENT_SQL)
    assert svc._history[svc._operator_session_id][-1].edited is False


def test_edited_sql_cannot_write(svc, alice):
    import pytest

    from escrowe.guard import Denied
    with pytest.raises(Denied):
        svc.run_edited(alice, "q", "DELETE FROM customers")


def test_the_cells_current_sql_reaches_the_agent_and_skips_replay(svc, alice):
    svc.agent = agent = RecordingAgent([f'{{"sql": "{AGENT_SQL}"}}', f'{{"sql": "{EDITED_SQL}"}}'])
    svc.ask(alice, "customers per tier")
    # The same question, but the cell's SQL was edited without being run: not a replay.
    svc.ask(alice, "customers per tier", current_sql=EDITED_SQL)
    assert len(agent.seen) == 2
    assert "CURRENT QUERY for this question" in agent.seen[-1] and EDITED_SQL in agent.seen[-1]
    # Unchanged SQL is still a replay: no agent call.
    svc.ask(alice, "customers per tier", current_sql=EDITED_SQL)
    assert len(agent.seen) == 2


def test_clearing_the_context_forgets_earlier_questions(svc, alice):
    svc.agent = agent = RecordingAgent([f'{{"sql": "{AGENT_SQL}"}}', f'{{"sql": "{AGENT_SQL}"}}'])
    svc.ask(alice, "customers per tier")
    svc.clear_history(alice)
    svc.ask(alice, "customers per tier")        # asked afresh, not replayed
    assert len(agent.seen) == 2 and "- Q: " not in agent.seen[-1]
