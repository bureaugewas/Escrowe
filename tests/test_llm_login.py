"""Connecting an LLM: two vendors, two ways in each, and one rule for which
of them actually answers a question."""

from __future__ import annotations

import re
import subprocess

import pytest

from escrowe import llm_login
from escrowe.agent import Agent
from escrowe.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "cat.sqlite")


@pytest.fixture(autouse=True)
def no_cli(monkeypatch):
    """No vendor CLI is installed unless a test says otherwise, and no test
    ever shells out to one."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(llm_login, "browser_status", lambda v: {"installed": False, "logged_in": False})


def signed_in(monkeypatch, *names: str) -> None:
    monkeypatch.setattr(llm_login, "browser_status",
                        lambda v: {"installed": True, "logged_in": v.name in names})


# ------------------------------------------------------------ the vendors

def test_both_vendors_are_offered():
    assert list(llm_login.VENDORS) == ["claude", "chatgpt"]


def test_each_provider_belongs_to_exactly_one_vendor():
    for v in llm_login.VENDORS.values():
        assert llm_login.for_provider(v.cli_provider) is v
        assert llm_login.for_provider(v.api_provider) is v
    assert llm_login.for_provider("mock") is None


def test_the_cli_status_replies_are_read_correctly():
    claude, chatgpt = llm_login.VENDORS["claude"], llm_login.VENDORS["chatgpt"]
    assert claude.signed_in(subprocess.CompletedProcess([], 0, '{"loggedIn": true}', ""))
    assert not claude.signed_in(subprocess.CompletedProcess([], 0, "not json", ""))
    # codex says it on stderr, and "Not logged in" contains "logged in".
    assert chatgpt.signed_in(subprocess.CompletedProcess([], 0, "", "Logged in using ChatGPT\n"))
    assert not chatgpt.signed_in(subprocess.CompletedProcess([], 1, "", "Not logged in\n"))
    assert not chatgpt.signed_in(subprocess.CompletedProcess([], 0, "", "Not logged in\n"))


# ------------------------------------------------------------ the api keys

def test_each_vendor_keeps_its_own_key(store):
    llm_login.save_api_key(store, llm_login.vendor("chatgpt"), "sk-openai")
    assert llm_login.stored_api_key(store, "chatgpt") == "sk-openai"
    assert llm_login.stored_api_key(store, "claude") is None


def test_a_key_can_come_from_the_environment(store, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    assert llm_login.stored_api_key(store, "chatgpt") == "sk-from-env"
    assert llm_login.stored_api_key(store, "claude") is None


def test_forgetting_one_vendor_leaves_the_other_alone(store):
    llm_login.save_api_key(store, llm_login.vendor("claude"), "sk-ant")
    llm_login.save_api_key(store, llm_login.vendor("chatgpt"), "sk-openai")
    llm_login.forget_api_key(store, llm_login.vendor("chatgpt"))
    assert llm_login.stored_api_key(store, "claude") == "sk-ant"
    assert llm_login.stored_api_key(store, "chatgpt") is None


def test_a_stored_key_never_appears_in_the_status(store):
    llm_login.save_api_key(store, llm_login.vendor("chatgpt"), "sk-openai-secret")
    st = llm_login.status(store, "chatgpt")
    assert "sk-openai-secret" not in str(st)
    assert st["api_key"] is True


# ----------------------------------------------------------- which way in

def test_nothing_connected_answers_nothing(store):
    st = llm_login.status(store, "chatgpt")
    assert st["source"] == "none" and st["provider"] == "none" and not st["connected"]


def test_not_now_wins_over_a_login_that_still_exists(store, monkeypatch):
    signed_in(monkeypatch, "chatgpt")
    store.set_setting(llm_login.VENDOR_SETTING, llm_login.NO_LLM)
    st = llm_login.status(store)
    assert st["source"] == "none" and st["provider"] == "none" and not st["connected"]


def test_a_browser_login_is_preferred_over_a_key_because_it_does_not_bill(store, monkeypatch):
    signed_in(monkeypatch, "chatgpt")
    llm_login.save_api_key(store, llm_login.vendor("chatgpt"), "sk-openai")
    assert llm_login.status(store, "chatgpt")["provider"] == "codex-cli"


def test_but_the_method_the_person_chose_wins(store, monkeypatch):
    signed_in(monkeypatch, "chatgpt")
    llm_login.save_api_key(store, llm_login.vendor("chatgpt"), "sk-openai")
    store.set_setting(llm_login.METHOD_SETTING, "api_key")
    assert llm_login.status(store, "chatgpt")["provider"] == "openai"


def test_a_chosen_method_that_no_longer_works_falls_back(store, monkeypatch):
    signed_in(monkeypatch, "chatgpt")
    store.set_setting(llm_login.METHOD_SETTING, "api_key")      # but no key is stored
    assert llm_login.status(store, "chatgpt")["source"] == "browser"


def test_the_chosen_vendor_is_the_one_reported(store, monkeypatch):
    signed_in(monkeypatch, "claude", "chatgpt")
    store.set_setting(llm_login.VENDOR_SETTING, "chatgpt")
    assert llm_login.status(store)["vendor"] == "chatgpt"


def test_with_no_choice_made_whichever_is_connected_answers(store, monkeypatch):
    signed_in(monkeypatch, "chatgpt")
    assert llm_login.status(store)["vendor"] == "chatgpt"


# ------------------------------------------------------- what the agent does

def test_the_agent_follows_the_connected_vendor(store, monkeypatch):
    signed_in(monkeypatch, "chatgpt")
    store.set_setting(llm_login.VENDOR_SETTING, "chatgpt")
    assert Agent(store=store).provider == "codex-cli"


def test_an_api_key_agent_picks_up_that_vendors_key_and_model(store):
    llm_login.save_api_key(store, llm_login.vendor("chatgpt"), "sk-openai")
    agent = Agent("openai", store=store)
    assert agent.api_key == "sk-openai"
    assert agent.model == llm_login.VENDORS["chatgpt"].model


def test_a_cli_agent_is_given_neither_a_key_nor_a_model(store):
    """It answers as whatever account and model that CLI is set to, so escrowe
    hands it neither - and claims neither in the transcript."""
    llm_login.save_api_key(store, llm_login.vendor("chatgpt"), "sk-openai")
    agent = Agent("codex-cli", store=store)
    assert agent.api_key is None and agent.model == ""


def test_an_explicit_model_wins_over_the_vendor_default(store):
    assert Agent("openai", "gpt-tiny", store=store).model == "gpt-tiny"


# ---------------------------------------------- talking to a CLI for real

def stub_cli(tmp_path, name: str, body: str) -> str:
    """A stand-in binary, so the CLI providers are exercised as processes."""
    path = tmp_path / name
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return str(path)


def test_codex_is_asked_on_stdin_and_its_last_message_is_the_reply(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_BIN", stub_cli(tmp_path, "codex", 'cat > "$0.prompt"; echo "SELECT 1"'))
    agent = Agent("codex-cli")
    assert agent._ask("SYS", "count the orders") == "SELECT 1"
    prompt = (tmp_path / "codex.prompt").read_text()
    assert "SYS" in prompt and "count the orders" in prompt


def test_codex_runs_read_only_and_outside_a_git_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_BIN", stub_cli(tmp_path, "codex", 'echo "$@" > "$0.argv"; echo hi'))
    Agent("codex-cli")._ask("SYS", "hello")
    argv = (tmp_path / "codex.argv").read_text()
    assert "--sandbox read-only" in argv and "--skip-git-repo-check" in argv


def test_a_signed_out_codex_is_reported_as_a_login_problem(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_BIN", stub_cli(tmp_path, "codex", 'echo "Not logged in" >&2; exit 1'))
    agent = Agent("codex-cli")
    assert agent._ask("SYS", "hello") is None
    assert agent.needs_login and "ChatGPT" in agent.last_error


def test_a_missing_cli_is_a_failure_not_a_crash(monkeypatch):
    monkeypatch.setenv("CODEX_BIN", "definitely-not-installed-xyz")
    agent = Agent("codex-cli")
    assert agent._ask("SYS", "hello") is None
    assert "not installed" in agent.last_error


# ------------------------------------------------- talking to the OpenAI SDK

class FakeResponses:
    """Stands in for client.responses, recording what it was asked for."""

    def __init__(self, text="SELECT 1"):
        self.text, self.kwargs = text, None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if not kwargs.get("stream"):
            return type("R", (), {"output_text": self.text})()
        return [type("E", (), {"type": "response.output_text.delta", "delta": chunk})()
                for chunk in re.findall(r"\S+\s*", self.text)]


def openai_agent(text="SELECT 1", **kw):
    agent = Agent("openai", api_key="sk-test", **kw)
    agent._client = type("C", (), {"responses": FakeResponses(text)})()
    return agent


def test_the_schema_is_the_instructions_and_the_question_is_the_input():
    agent = openai_agent()
    assert agent._ask("SCHEMA HERE", "count the orders") == "SELECT 1"
    assert agent._client.responses.kwargs["instructions"] == "SCHEMA HERE"
    assert agent._client.responses.kwargs["input"] == "count the orders"
    assert agent._client.responses.kwargs["model"] == llm_login.VENDORS["chatgpt"].model


def test_a_streamed_reply_arrives_token_by_token():
    agent, seen = openai_agent("SELECT count(*)"), []
    assert agent._ask("SYS", "how many?", seen.append) == "SELECT count(*)"
    assert seen == ["SELECT ", "count(*)"]


def test_a_thinking_budget_asks_openai_for_more_effort():
    agent = openai_agent(thinking_budget=8000)
    agent._ask("SYS", "hello")
    assert agent._client.responses.kwargs["reasoning"] == {"effort": "high"}
    assert agent._client.responses.kwargs["max_output_tokens"] >= 8000


def test_an_empty_reply_becomes_a_refusal_not_an_empty_answer():
    from escrowe.agent import parse_reply
    assert parse_reply(openai_agent("")._ask("SYS", "hello")).answer.startswith("The model returned nothing")
