"""LLM providers for SQL generation and policy review.

Providers, in order of preference when llm_provider is "auto":
  1. claude  — the Claude Code CLI (`claude -p`), using your existing CLI
               login. No API key configuration needed.
  2. ollama  — local Ollama HTTP API.
  3. mock    — returns uncertain (confidence 0.3) so the human approval
               queue always gets exercised in demo mode.
"""

import json
import os
import shutil
import subprocess
from typing import Optional

import httpx

import config

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3")
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")


# ---------------------------------------------------------------- providers

def claude_available() -> bool:
    return shutil.which(CLAUDE_BIN) is not None


def ollama_available() -> bool:
    try:
        return httpx.get(f"{OLLAMA_URL}/api/tags", timeout=1.5).status_code == 200
    except Exception:
        return False


def resolve_provider() -> str:
    pref = config.load_config().get("llm_provider", "auto")
    if pref in ("claude", "ollama", "mock"):
        return pref
    if claude_available():
        return "claude"
    if ollama_available():
        return "ollama"
    return "mock"


def status() -> dict:
    return {
        "claude_cli": claude_available(),
        "ollama": ollama_available(),
        "configured": config.load_config().get("llm_provider", "auto"),
        "active": resolve_provider(),
    }


def _call_claude(prompt: str) -> Optional[str]:
    try:
        proc = subprocess.run(
            [CLAUDE_BIN, "-p", prompt, "--output-format", "json"],
            capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            return None
        return json.loads(proc.stdout).get("result")
    except Exception:
        return None


def _call_ollama(prompt: str) -> Optional[str]:
    try:
        resp = httpx.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": prompt,
                  "stream": False, "format": "json"},
            timeout=60.0)
        resp.raise_for_status()
        return resp.json().get("response", "")
    except Exception:
        return None


def _extract_json(text: Optional[str]) -> Optional[dict]:
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except Exception:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except Exception:
                return None
    return None


def ask_json(prompt: str):
    """Send a prompt to the active provider, expect JSON back.
    Returns (parsed_dict_or_None, provider_name)."""
    provider = resolve_provider()
    if provider == "claude":
        return _extract_json(_call_claude(prompt)), "claude"
    if provider == "ollama":
        return _extract_json(_call_ollama(prompt)), "ollama"
    return None, "mock"


# ----------------------------------------------------------- policy review

REVIEWER_PROMPT = """You are a data governance policy reviewer. An AI agent has submitted a SQL query.
Your job is to determine whether this query complies with the agent's access policy.

AGENT ROLE: {role}
ROLE DESCRIPTION: {description}

SUBMITTED QUERY:
{sql}

PARSED QUERY DETAILS:
- Tables accessed: {tables}
- Join conditions: {joins}
- Aggregations used: {aggregations}
- Row-level columns in SELECT: {row_columns}

POLICY CONTEXT:
{relevant_policy_excerpt}

INITIAL AUTOMATED DECISION: {initial_decision} (confidence: {score})
REASON FOR ESCALATION: {reason}

Respond in JSON only:
{{
  "decision": "allow" | "deny" | "uncertain",
  "reasoning": "one sentence explanation",
  "confidence": 0.0-1.0,
  "suggested_rewrite": "optional safer SQL if applicable"
}}"""


def _mock_review() -> dict:
    return {
        "decision": "uncertain",
        "reasoning": "No LLM reviewer available; cannot evaluate the query.",
        "confidence": 0.3,
        "suggested_rewrite": None,
        "reviewer": "mock",
    }


def review_query(role: str, description: str, sql: str, tables: list,
                 joins: list, aggregations: list, row_columns: list,
                 policy_excerpt: str, initial_decision: str,
                 score: float, reason: str) -> dict:
    prompt = REVIEWER_PROMPT.format(
        role=role, description=description, sql=sql,
        tables=", ".join(tables) or "none",
        joins="; ".join(joins) or "none",
        aggregations=", ".join(aggregations) or "none",
        row_columns=", ".join(row_columns) or "none",
        relevant_policy_excerpt=policy_excerpt,
        initial_decision=initial_decision, score=score, reason=reason)
    parsed, provider = ask_json(prompt)
    if parsed is None:
        return _mock_review()
    decision = parsed.get("decision", "uncertain")
    if decision not in ("allow", "deny", "uncertain"):
        decision = "uncertain"
    try:
        confidence = max(0.0, min(1.0, float(parsed.get("confidence", 0.3))))
    except (TypeError, ValueError):
        confidence = 0.3
    return {
        "decision": decision,
        "reasoning": str(parsed.get("reasoning", "")).strip() or "No reasoning given.",
        "confidence": confidence,
        "suggested_rewrite": parsed.get("suggested_rewrite") or None,
        "reviewer": provider,
    }


# ----------------------------------------------------------- NL → SQL agent

NL2SQL_PROMPT = """You translate a natural language analytics question into a single DuckDB SELECT query.

DATABASE SCHEMA:
{schema}

GOVERNANCE POLICY for the requesting agent role — the query MUST comply with it:
{policy}

{feedback}QUESTION: {question}

Rules:
- One SELECT statement only, DuckDB dialect, no comments.
- Respect the policy above: use aggregations where required, avoid prohibited tables and joins.
- If the question fundamentally cannot be answered within the policy, do not generate SQL.

Respond in JSON only, with exactly one of these shapes:
{{"sql": "the SQL query"}}
{{"refusal": "one sentence explaining which policy rule prevents answering this question"}}"""


def generate_sql(question: str, schema: str, policy: str,
                 feedback: Optional[str] = None) -> dict:
    fb = ""
    if feedback:
        fb = ("A previous attempt was rejected by the governance engine for this "
              f"reason: {feedback}\nGenerate a compliant alternative that still "
              "answers the question.\n\n")
    prompt = NL2SQL_PROMPT.format(schema=schema, policy=policy,
                                  feedback=fb, question=question)
    parsed, provider = ask_json(prompt)
    if parsed:
        if parsed.get("sql"):
            return {"sql": str(parsed["sql"]), "source": provider}
        refusal = parsed.get("refusal") or parsed.get("reason") or parsed.get("error")
        if refusal:
            return {"sql": None, "refusal": str(refusal), "source": provider}
    return {"sql": None, "source": "mock",
            "note": "No LLM available to translate the question. Configure the "
                    "Claude CLI or Ollama in Settings."}
