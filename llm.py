"""Ollama client for the policy reviewer LLM, with a mock fallback.

If Ollama is unreachable, the mock returns decision=uncertain with
confidence 0.3 so the human approval queue always gets exercised in
demo mode.
"""

import json
import os

import httpx

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3")

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
- Estimated result rows: {estimated_rows}

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
        "reasoning": "Ollama is not available; mock reviewer cannot evaluate the query.",
        "confidence": 0.3,
        "suggested_rewrite": None,
        "reviewer": "mock",
    }


def review_query(role: str, description: str, sql: str, tables: list,
                 joins: list, aggregations: list, row_columns: list,
                 estimated_rows, policy_excerpt: str,
                 initial_decision: str, score: float, reason: str) -> dict:
    """Ask the policy reviewer LLM for a second opinion. Falls back to mock."""
    prompt = REVIEWER_PROMPT.format(
        role=role,
        description=description,
        sql=sql,
        tables=", ".join(tables) or "none",
        joins="; ".join(joins) or "none",
        aggregations=", ".join(aggregations) or "none",
        row_columns=", ".join(row_columns) or "none",
        estimated_rows=estimated_rows if estimated_rows is not None else "unknown",
        relevant_policy_excerpt=policy_excerpt,
        initial_decision=initial_decision,
        score=score,
        reason=reason,
    )
    try:
        resp = httpx.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": prompt,
                  "stream": False, "format": "json"},
            timeout=60.0,
        )
        resp.raise_for_status()
        raw = resp.json().get("response", "")
        parsed = json.loads(raw)
        decision = parsed.get("decision", "uncertain")
        if decision not in ("allow", "deny", "uncertain"):
            decision = "uncertain"
        return {
            "decision": decision,
            "reasoning": str(parsed.get("reasoning", "")).strip() or "No reasoning given.",
            "confidence": max(0.0, min(1.0, float(parsed.get("confidence", 0.3)))),
            "suggested_rewrite": parsed.get("suggested_rewrite") or None,
            "reviewer": "llm",
        }
    except Exception:
        return _mock_review()


NL2SQL_PROMPT = """You translate natural language analytics questions into DuckDB SQL.

Available tables:
- employees(id, name, department, hire_date, salary)
- sales_transactions(id, employee_id, customer_id, amount, date, product_id)
- customers(id, name, region, account_tier)
- products(id, name, category, unit_price)
- payroll(employee_id, gross, net, period)

QUESTION: {question}

Respond in JSON only: {{"sql": "the SQL query"}}"""


def generate_sql(question: str) -> dict:
    """Translate natural language to SQL via Ollama. Falls back to a mock."""
    try:
        resp = httpx.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": OLLAMA_MODEL,
                  "prompt": NL2SQL_PROMPT.format(question=question),
                  "stream": False, "format": "json"},
            timeout=60.0,
        )
        resp.raise_for_status()
        parsed = json.loads(resp.json().get("response", ""))
        return {"sql": parsed.get("sql", ""), "source": "llm"}
    except Exception:
        return {
            "sql": "SELECT COUNT(*) AS n, AVG(amount) AS avg_amount FROM sales_transactions",
            "source": "mock",
            "note": "Ollama unavailable — returned an example query instead of a translation.",
        }
