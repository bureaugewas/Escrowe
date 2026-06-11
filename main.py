"""Escrow PoC — FastAPI app, all routes.

Run with: uvicorn main:app --reload --port 8765
"""

import hashlib
import os
import time
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db
import governance
import llm

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(title="Escrow — Local AI Data Governance PoC")


class QueryRequest(BaseModel):
    role: str
    sql: str


class NLRequest(BaseModel):
    question: str


class QueueDecision(BaseModel):
    approve: bool
    rewritten_sql: Optional[str] = None


def _audit(role: str, sql: str, decision: str, confidence: float,
           decided_by: str, reasoning: str, duration_ms: float) -> None:
    conn = db.get_audit_conn()
    qhash = hashlib.sha256(sql.encode()).hexdigest()[:12]
    conn.execute(
        "INSERT INTO audit_log (agent_role, query_hash, sql_text, decision, "
        "confidence, decided_by, reasoning, duration_ms) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [role, qhash, sql, decision, confidence, decided_by, reasoning, duration_ms])


def _execute(sql: str, max_rows: Optional[int]):
    conn = db.get_data_conn()
    cursor = conn.execute(sql)
    columns = [d[0] for d in cursor.description]
    rows = cursor.fetchall()
    if max_rows is not None and len(rows) > max_rows:
        return None, columns, len(rows)
    return [list(r) for r in rows], columns, len(rows)


@app.get("/")
def index():
    return FileResponse(os.path.join(BASE_DIR, "static", "index.html"))


@app.get("/api/roles")
def roles():
    policies = governance.load_policies()
    return {name: {"description": cfg.get("description", "")}
            for name, cfg in policies["roles"].items()}


@app.get("/api/policy/{role}")
def policy(role: str):
    policies = governance.load_policies()
    if role not in policies["roles"]:
        raise HTTPException(404, f"Unknown role '{role}'")
    return {"role": role, "policy": policies["roles"][role],
            "ethics_rules": policies["ethics"]}


@app.post("/api/generate_sql")
def generate_sql(req: NLRequest):
    return llm.generate_sql(req.question)


@app.post("/api/query")
def submit_query(req: QueryRequest):
    start = time.time()
    policies = governance.load_policies()
    result = governance.evaluate_query(req.sql, req.role, policies)
    pq = result.parsed or governance.ParsedQuery()
    decided_by = "engine"
    llm_review = None

    # Ethics escalation goes straight to the human queue
    if result.decision == "escalate_human":
        return _enqueue(req, result, decided_by, start)

    # Low confidence → second opinion from the policy reviewer LLM
    if result.confidence < governance.LLM_THRESHOLD:
        role_cfg = policies["roles"].get(req.role, {})
        llm_review = llm.review_query(
            role=req.role,
            description=role_cfg.get("description", ""),
            sql=req.sql,
            tables=pq.tables, joins=pq.joins,
            aggregations=pq.aggregations, row_columns=pq.row_columns,
            estimated_rows=None,
            policy_excerpt=governance.policy_excerpt_for(req.role, pq.tables, policies),
            initial_decision=result.decision,
            score=result.confidence,
            reason="; ".join(result.reasoning),
        )
        result.reasoning.append(
            f"LLM reviewer ({llm_review['reviewer']}): {llm_review['reasoning']}")
        if llm_review["decision"] in ("allow", "deny") and \
                llm_review["confidence"] >= governance.HUMAN_THRESHOLD:
            result.decision = llm_review["decision"]
            result.confidence = llm_review["confidence"]
            if llm_review.get("suggested_rewrite"):
                result.suggested_rewrite = llm_review["suggested_rewrite"]
            decided_by = "llm"
        else:
            result.confidence = min(result.confidence, llm_review["confidence"])

        # Still uncertain after LLM review → human queue
        if result.confidence < governance.HUMAN_THRESHOLD:
            return _enqueue(req, result, "llm", start)

    duration = (time.time() - start) * 1000
    reasoning = " ".join(result.reasoning)

    if result.decision == "deny":
        _audit(req.role, req.sql, "denied", result.confidence, decided_by,
               reasoning, duration)
        return {"decision": "denied", "confidence": result.confidence,
                "reasoning": result.reasoning,
                "triggered_rules": result.triggered_rules,
                "suggested_rewrite": result.suggested_rewrite,
                "decided_by": decided_by}

    # ALLOW → execute on DuckDB
    max_rows = policies["roles"][req.role].get("max_result_rows")
    try:
        rows, columns, n = _execute(req.sql, max_rows)
    except Exception as e:
        duration = (time.time() - start) * 1000
        _audit(req.role, req.sql, "denied", result.confidence, decided_by,
               f"Execution error: {e}", duration)
        return {"decision": "error", "reasoning": [f"Execution error: {e}"],
                "decided_by": decided_by}

    if rows is None:
        msg = (f"Result has {n} rows, exceeding max_result_rows={max_rows} "
               f"for role '{req.role}'.")
        result.reasoning.append(msg)
        duration = (time.time() - start) * 1000
        _audit(req.role, req.sql, "denied", result.confidence, decided_by,
               " ".join(result.reasoning), duration)
        return {"decision": "denied", "confidence": result.confidence,
                "reasoning": result.reasoning,
                "triggered_rules": result.triggered_rules + ["max_result_rows"],
                "decided_by": decided_by}

    duration = (time.time() - start) * 1000
    _audit(req.role, req.sql, "allowed", result.confidence, decided_by,
           reasoning, duration)
    return {"decision": "allowed", "confidence": result.confidence,
            "reasoning": result.reasoning,
            "triggered_rules": result.triggered_rules,
            "columns": columns, "rows": rows, "row_count": n,
            "decided_by": decided_by}


def _enqueue(req: QueryRequest, result, decided_by: str, start: float):
    conn = db.get_audit_conn()
    pq = result.parsed or governance.ParsedQuery()
    reasoning = " ".join(result.reasoning)
    row = conn.execute(
        "INSERT INTO approval_queue (agent_role, sql_text, tables_involved, "
        "llm_reasoning) VALUES (?, ?, ?, ?) RETURNING id",
        [req.role, req.sql, ", ".join(pq.tables), reasoning]).fetchone()
    duration = (time.time() - start) * 1000
    _audit(req.role, req.sql, "escalated", result.confidence, decided_by,
           reasoning, duration)
    return {"decision": "pending", "queue_id": row[0],
            "confidence": result.confidence, "reasoning": result.reasoning,
            "triggered_rules": result.triggered_rules,
            "decided_by": decided_by}


@app.get("/api/queue")
def queue():
    conn = db.get_audit_conn()
    rows = conn.execute(
        "SELECT id, ts, agent_role, sql_text, tables_involved, llm_reasoning "
        "FROM approval_queue WHERE status = 'pending' ORDER BY ts").fetchall()
    return [{"id": r[0], "ts": str(r[1]), "role": r[2], "sql": r[3],
             "tables": r[4], "reasoning": r[5]} for r in rows]


@app.post("/api/queue/{item_id}/decision")
def decide(item_id: int, body: QueueDecision):
    conn = db.get_audit_conn()
    row = conn.execute(
        "SELECT agent_role, sql_text FROM approval_queue "
        "WHERE id = ? AND status = 'pending'", [item_id]).fetchone()
    if not row:
        raise HTTPException(404, "Queue item not found or already decided")
    role, original_sql = row
    final_sql = (body.rewritten_sql or original_sql).strip()
    status = "approved" if body.approve else "denied"
    conn.execute(
        "UPDATE approval_queue SET status = ?, decided_ts = current_timestamp, "
        "final_sql = ? WHERE id = ?", [status, final_sql, item_id])

    if not body.approve:
        _audit(role, original_sql, "denied", 1.0, "human",
               "Denied by human approver.", 0)
        return {"decision": "denied", "decided_by": "human"}

    policies = governance.load_policies()
    max_rows = policies["roles"].get(role, {}).get("max_result_rows")
    note = "Approved by human approver."
    if final_sql != original_sql.strip():
        note += " SQL was rewritten before approval."
    try:
        rows, columns, n = _execute(final_sql, max_rows)
    except Exception as e:
        _audit(role, final_sql, "approved", 1.0, "human",
               f"{note} Execution error: {e}", 0)
        return {"decision": "error", "reasoning": [f"Execution error: {e}"],
                "decided_by": "human"}
    _audit(role, final_sql, "approved", 1.0, "human", note, 0)
    return {"decision": "allowed", "decided_by": "human",
            "columns": columns, "rows": rows, "row_count": n,
            "reasoning": [note]}


@app.get("/api/audit")
def audit(role: Optional[str] = None, decision: Optional[str] = None, limit: int = 200):
    conn = db.get_audit_conn()
    sql = ("SELECT ts, agent_role, query_hash, sql_text, decision, confidence, "
           "decided_by, reasoning, duration_ms FROM audit_log WHERE 1=1")
    params: list = []
    if role:
        sql += " AND agent_role = ?"
        params.append(role)
    if decision:
        sql += " AND decision = ?"
        params.append(decision)
    sql += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [{"ts": str(r[0]), "role": r[1], "query_hash": r[2], "sql": r[3],
             "decision": r[4], "confidence": r[5], "decided_by": r[6],
             "reasoning": r[7], "duration_ms": r[8]} for r in rows]


app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")),
          name="static")


@app.on_event("startup")
def startup():
    db.get_data_conn()
    db.get_audit_conn()
