"""The blind guarantee: the agent never receives data, only metadata and shape
feedback (SQL errors, row counts). Enforced by wiring, verified here by
recording everything the agent is ever given."""

import pytest

from escrowe.agent import Agent, AgentResult
from escrowe.guard import Denied


class RecordingAgent(Agent):
    def __init__(self, scripted):
        super().__init__("mock")
        self.scripted = list(scripted)
        self.seen = []           # every (system, user) prompt the agent was shown

    def _ask(self, system, user):
        self.seen.append(system + "\n" + user)
        return self.scripted.pop(0) if self.scripted else '{"refusal": "out of ideas"}'


def all_customer_names(svc, bob):
    return {r["name"] for r in svc.sql(bob, "SELECT name FROM customers").table.to_pylist()}


def test_agent_sees_metadata_and_feedback_but_never_rows(svc, alice, bob):
    names = all_customer_names(svc, bob)
    agent = RecordingAgent([
        '{"sql": "SELECT * FROM employees"}',                              # denied: alice has no such table
        '{"sql": "SELECT nonexistent FROM customers"}',                    # denied: column does not exist
        '{"sql": "SELECT tier, count(*) AS n FROM customers GROUP BY tier"}',
    ])
    svc.agent = agent
    res = svc.ask(alice, "how many customers per tier?")
    assert res.attempts == 3 and res.table.num_rows == 3
    # shape feedback reached the agent…
    assert "employees" in agent.seen[1].lower() or "does not exist" in agent.seen[1].lower() \
        or "catalog" in agent.seen[1].lower()
    # …but no data ever did: not a single real customer name in anything the agent saw
    blob = "\n".join(agent.seen)
    assert not any(n in blob for n in names)
    # the agent gets names, types and comments - never values
    assert "TABLE customers" in blob and "tier VARCHAR" in blob and "Customer master data" in blob


def test_a_reply_that_is_not_a_query_is_surfaced_as_prose(svc, alice):
    """When the agent explains rather than queries - including saying it cannot -
    that is an answer shown to the person, not a hard denial."""
    from escrowe.service import Answer
    svc.agent = RecordingAgent(['The schema has no revenue target to compare against.'])
    res = svc.ask(alice, "are we on target?")
    assert isinstance(res, Answer)
    assert "revenue target" in res.text
    assert svc.store.audit_rows(alice.user)[0]["decision"] == "answered"


def test_agent_result_never_contains_rows_type():
    """AgentResult must stay incapable of carrying data back from a query.
    Adding a field here is fine; adding one that could hold rows is not."""
    fields = AgentResult.__dataclass_fields__
    assert set(fields) == {"sql", "refusal", "answer", "provider", "attempts", "needs_login"}
    assert all(f.type in ("str | None", "str", "bool", "list[Attempt]") for f in fields.values()), \
        {n: f.type for n, f in fields.items()}


def test_metadata_never_reads_data(svc, alice, monkeypatch):
    """None of a table's actual values may reach the agent. The catalog was
    already snapshotted once when svc connected (see _connect); reading it
    back for the agent must issue no further query of any kind."""
    from escrowe.metadata import Metadata

    executed = []
    original = svc.engine.execute
    monkeypatch.setattr(svc.engine, "execute", lambda sql, *a, **k: (executed.append(sql), original(sql, *a, **k))[1])

    text = Metadata.render(svc.catalog_for(alice))
    assert executed == [], f"metadata issued queries: {executed}"
    assert "Acme" not in text and "Globex" not in text            # real row values
    assert "customers" in text and "tier VARCHAR" in text and "Customer master data" in text


def test_transcript_records_exactly_what_was_sent(svc, alice, tmp_path):
    """The log is the evidence for the blind-agent claim, so it must hold the real
    prompt, written as it is sent, not a reconstruction afterwards."""
    from escrowe.transcript import Transcript, default_path

    svc.agent = RecordingAgent(['{"sql": "SELECT tier, count(*) AS n FROM customers GROUP BY tier"}'])
    svc.agent.transcript = svc.transcript
    res = svc.ask(alice, "how many customers per tier?")
    assert res.table.num_rows == 3

    entries = Transcript(default_path(svc.settings.home)).read()
    assert len(entries) == 1
    e = entries[0]
    assert e["user"] == alice.user
    assert e["question"] == "how many customers per tier?" and e["attempt"] == 1
    # byte-for-byte what the agent was shown
    assert e["sent"]["system"] + "\n" + e["sent"]["user"] == svc.agent.seen[0]
    assert e["received"] == '{"sql": "SELECT tier, count(*) AS n FROM customers GROUP BY tier"}'
    assert e["sent_chars"] == len(e["sent"]["system"]) + len(e["sent"]["user"])


def test_transcript_proves_no_data_reached_the_model(svc, alice, bob):
    """Read the log back and check it against the real values in the database."""
    from escrowe.transcript import Transcript, default_path

    names = all_customer_names(svc, bob)
    svc.agent = RecordingAgent(['{"sql": "SELECT count(*) AS n FROM customers"}'])
    svc.agent.transcript = svc.transcript
    svc.ask(alice, "how many customers?")

    everything = "\n".join(
        e["sent"]["system"] + e["sent"]["user"]
        for e in Transcript(default_path(svc.settings.home)).read())
    assert not any(n in everything for n in names)


def test_a_failed_call_is_still_recorded(svc, alice):
    """A prompt that errored was still shown to a model, so it belongs in the log."""
    from escrowe.agent import Agent
    from escrowe.transcript import Transcript, default_path

    class Broken(Agent):
        def _ask(self, system, user):
            raise RuntimeError("provider exploded")

    svc.agent = Broken("mock")
    svc.agent.transcript = svc.transcript
    with pytest.raises(Exception):
        svc.ask(alice, "anything")
    entries = Transcript(default_path(svc.settings.home)).read()
    assert entries and "provider exploded" in (entries[-1]["error"] or "")
    assert entries[-1]["sent"]["system"]


def test_the_agent_can_only_see_what_the_account_can_see(svc, alice, bob):
    """There is no escrowe-side grant: alice's account simply doesn't have
    `employees` in its own catalog, the same way a real database would leave
    it out for an ungranted account."""
    from escrowe.metadata import Metadata
    alice_text = Metadata.render(svc.catalog_for(alice))
    assert "employees" not in alice_text and "customers" in alice_text
    bob_text = Metadata.render(svc.catalog_for(bob))
    assert "employees" in bob_text


def test_the_catalog_is_read_once_at_connect_and_never_again(svc, alice, monkeypatch):
    """The queries that build the agent's schema (catalog/table_sizes) are
    fixed and run only when a connection is made - not once per question, and
    never reachable from anything the agent writes."""
    calls = []
    original = type(svc.engine).catalog
    monkeypatch.setattr(type(svc.engine), "catalog", lambda self: (calls.append(1), original(self))[1])

    svc.agent = RecordingAgent(['{"sql": "SELECT count(*) AS n FROM customers"}'])
    svc.ask(alice, "how many customers?")
    svc.ask(alice, "how many customers, again?")
    svc.sql(alice, "SELECT count(*) AS n FROM customers")
    assert calls == []          # not once, across three separate queries


def test_the_agent_cannot_write(svc, alice):
    """ask() always runs with allow_write=False, so a write the agent proposes
    is refused before it ever reaches the database."""
    svc.settings.agent_attempts = 1
    svc.agent = RecordingAgent(['{"sql": "DELETE FROM customers"}'])
    with pytest.raises(Denied, match="only read"):
        svc.ask(alice, "delete everything")
