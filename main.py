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

import config
import db
import governance
import llm
import pii

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(title="Escrow — Local AI Data Governance PoC")

MAX_SQL_ATTEMPTS = 3  # NL question → SQL retries when governance rejects


class AskRequest(BaseModel):
    role: str
    question: str


class QueryRequest(BaseModel):
    role: str
    sql: str


class QueueDecision(BaseModel):
    approve: bool
    rewritten_sql: Optional[str] = None


class ConfigUpdate(BaseModel):
    database_path: Optional[str] = None
    llm_provider: Optional[str] = None


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


# --------------------------------------------------------------- governance

def _process_query(role: str, sql: str) -> dict:
    """Full governance chain for one SQL statement: engine → LLM reviewer →
    human queue → execution with PII masking. Used by /api/query and /api/ask."""
    start = time.time()
    policies = governance.load_policies()
    result = governance.evaluate_query(sql, role, policies)
    pq = result.parsed or governance.ParsedQuery()
    decided_by = "engine"

    # Ethics escalation goes straight to the human queue
    if result.decision == "escalate_human":
        return _enqueue(role, sql, result, decided_by, start)

    # Low confidence → second opinion from the policy reviewer LLM
    if result.confidence < governance.LLM_THRESHOLD:
        role_cfg = policies["roles"].get(role, {})
        review = llm.review_query(
            role=role, description=role_cfg.get("description", ""), sql=sql,
            tables=pq.tables, joins=pq.joins,
            aggregations=pq.aggregations, row_columns=pq.row_columns,
            policy_excerpt=governance.policy_excerpt_for(role, pq.tables, policies),
            initial_decision=result.decision, score=result.confidence,
            reason="; ".join(result.reasoning))
        result.reasoning.append(
            f"Policy reviewer ({review['reviewer']}): {review['reasoning']}")
        if review["decision"] in ("allow", "deny") and \
                review["confidence"] >= governance.HUMAN_THRESHOLD:
            result.decision = review["decision"]
            result.confidence = review["confidence"]
            if review.get("suggested_rewrite"):
                result.suggested_rewrite = review["suggested_rewrite"]
            decided_by = "llm"
        else:
            result.confidence = min(result.confidence, review["confidence"])

        # Still uncertain after LLM review → human queue
        if result.confidence < governance.HUMAN_THRESHOLD:
            return _enqueue(role, sql, result, "llm", start)

    duration = (time.time() - start) * 1000
    reasoning = " ".join(result.reasoning)

    if result.decision == "deny":
        _audit(role, sql, "denied", result.confidence, decided_by,
               reasoning, duration)
        return {"decision": "denied", "confidence": result.confidence,
                "reasoning": result.reasoning,
                "triggered_rules": result.triggered_rules,
                "suggested_rewrite": result.suggested_rewrite,
                "decided_by": decided_by}

    # ALLOW → execute on DuckDB
    max_rows = policies["roles"][role].get("max_result_rows")
    try:
        rows, columns, n = _execute(sql, max_rows)
    except Exception as e:
        duration = (time.time() - start) * 1000
        _audit(role, sql, "denied", result.confidence, decided_by,
               f"Execution error: {e}", duration)
        return {"decision": "error", "reasoning": [f"Execution error: {e}"],
                "decided_by": decided_by}

    if rows is None:
        msg = (f"Result has {n} rows, exceeding max_result_rows={max_rows} "
               f"for role '{role}'.")
        result.reasoning.append(msg)
        duration = (time.time() - start) * 1000
        _audit(role, sql, "denied", result.confidence, decided_by,
               " ".join(result.reasoning), duration)
        return {"decision": "denied", "confidence": result.confidence,
                "reasoning": result.reasoning,
                "triggered_rules": result.triggered_rules + ["max_result_rows"],
                "decided_by": decided_by}

    registry = pii.load_registry(db.get_data_conn())
    rows, masked_cols = pii.mask_result(columns, rows, registry)

    duration = (time.time() - start) * 1000
    _audit(role, sql, "allowed", result.confidence, decided_by,
           reasoning, duration)
    return {"decision": "allowed", "confidence": result.confidence,
            "reasoning": result.reasoning,
            "triggered_rules": result.triggered_rules,
            "columns": columns, "rows": rows, "row_count": n,
            "masked_columns": masked_cols,
            "decided_by": decided_by}


def _enqueue(role: str, sql: str, result, decided_by: str, start: float) -> dict:
    conn = db.get_audit_conn()
    pq = result.parsed or governance.ParsedQuery()
    reasoning = " ".join(result.reasoning)
    row = conn.execute(
        "INSERT INTO approval_queue (agent_role, sql_text, tables_involved, "
        "llm_reasoning) VALUES (?, ?, ?, ?) RETURNING id",
        [role, sql, ", ".join(pq.tables), reasoning]).fetchone()
    duration = (time.time() - start) * 1000
    _audit(role, sql, "escalated", result.confidence, decided_by,
           reasoning, duration)
    return {"decision": "pending", "queue_id": row[0],
            "confidence": result.confidence, "reasoning": result.reasoning,
            "triggered_rules": result.triggered_rules,
            "decided_by": decided_by}


# ---------------------------------------------------------------- ask (NL)

@app.post("/api/ask")
def ask(req: AskRequest):
    """Natural-language question → Claude generates SQL behind the scenes →
    governance chain → results. The SQL is never returned to the caller;
    it is only visible in the audit log and the human approval queue."""
    try:
        schema = db.schema_text()
    except Exception as e:
        return {"decision": "error",
                "reasoning": [f"Cannot read the configured database: {e}"]}
    policy = governance.policy_excerpt_for(req.role, [])
    feedback = None
    last = None
    for attempt in range(1, MAX_SQL_ATTEMPTS + 1):
        gen = llm.generate_sql(req.question, schema, policy, feedback)
        if not gen.get("sql"):
            if gen.get("refusal"):
                _audit(req.role, f"[question] {req.question}", "denied", 0.9,
                       "llm", gen["refusal"], 0)
                return {"decision": "denied", "confidence": 0.9,
                        "reasoning": [gen["refusal"]],
                        "decided_by": "llm", "attempts": attempt,
                        "generator": gen["source"]}
            return {"decision": "error",
                    "reasoning": [gen.get("note", "SQL generation failed.")]}
        last = _process_query(req.role, gen["sql"])
        last.pop("suggested_rewrite", None)   # never expose SQL in ask mode
        last["attempts"] = attempt
        last["generator"] = gen["source"]
        if last["decision"] in ("allowed", "pending"):
            return last
        feedback = " ".join(last.get("reasoning", []))
    return last


# ------------------------------------------------------- raw SQL (advanced)

@app.post("/api/query")
def submit_query(req: QueryRequest):
    return _process_query(req.role, req.sql)


# ------------------------------------------------------------- explorer

@app.get("/api/schema")
def schema():
    try:
        conn = db.get_data_conn()
        tables = db.get_schema()
    except Exception as e:
        raise HTTPException(400, f"Cannot open database: {e}")
    registry = pii.load_registry(conn)
    for t in tables:
        cols = registry.get(t["name"].lower(), {})
        for c in t["columns"]:
            cat = cols.get(c["name"].lower())
            if cat:
                c["pii"] = cat
    return {"path": db.current_data_path(),
            "is_sample": db.current_data_path() == db.SAMPLE_DB,
            "tables": tables}


@app.get("/api/preview/{table}")
def preview(table: str, limit: int = 8):
    conn = db.get_data_conn()
    valid = {t["name"] for t in db.get_schema()}
    if table not in valid:
        raise HTTPException(404, f"Unknown table '{table}'")
    cursor = conn.execute(f'SELECT * FROM "{table}" LIMIT ?', [min(limit, 50)])
    columns = [d[0] for d in cursor.description]
    rows = [list(r) for r in cursor.fetchall()]
    registry = pii.load_registry(conn)
    table_pii = registry.get(table.lower(), {})
    idxs = {i: table_pii[c.lower()] for i, c in enumerate(columns)
            if c.lower() in table_pii}
    masked = [[pii.mask_value(v) if i in idxs else v for i, v in enumerate(row)]
              for row in rows]
    return {"table": table, "columns": columns, "rows": masked,
            "pii_columns": {columns[i]: cat for i, cat in idxs.items()}}


# ------------------------------------------------------------- settings

@app.get("/api/config")
def get_config():
    cfg = config.load_config()
    return {"database_path": cfg.get("database_path"),
            "effective_path": db.current_data_path(),
            "llm_provider": cfg.get("llm_provider", "auto"),
            "llm_status": llm.status()}


@app.post("/api/config")
def set_config(body: ConfigUpdate):
    if body.llm_provider is not None:
        if body.llm_provider not in ("auto", "claude", "ollama", "mock"):
            raise HTTPException(400, "llm_provider must be auto/claude/ollama/mock")
        config.save_config({"llm_provider": body.llm_provider})
    if body.database_path is not None:
        path = body.database_path.strip() or None
        try:
            db.switch_database(path)
        except Exception as e:
            raise HTTPException(400, f"Could not open database: {e}")
    return get_config()


@app.get("/api/browse")
def browse(path: Optional[str] = None):
    """Minimal server-side file browser for picking a .duckdb file."""
    target = os.path.abspath(os.path.expanduser(path or "~"))
    if not os.path.isdir(target):
        raise HTTPException(400, f"Not a directory: {target}")
    dirs, files = [], []
    try:
        for name in sorted(os.listdir(target)):
            if name.startswith("."):
                continue
            full = os.path.join(target, name)
            if os.path.isdir(full):
                dirs.append(name)
            elif name.endswith(".duckdb"):
                files.append(name)
    except PermissionError:
        raise HTTPException(403, f"No permission to read {target}")
    return {"path": target, "parent": os.path.dirname(target),
            "dirs": dirs, "files": files}


# ------------------------------------------------------- policies & queue

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
    if rows is not None:
        registry = pii.load_registry(db.get_data_conn())
        rows, _ = pii.mask_result(columns, rows, registry)
    _audit(role, final_sql, "approved", 1.0, "human", note, 0)
    return {"decision": "allowed", "decided_by": "human",
            "columns": columns, "rows": rows or [], "row_count": n,
            "reasoning": [note]}


@app.get("/api/audit")
def audit(role: Optional[str] = None, decision: Optional[str] = None,
          limit: int = 200):
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


@app.get("/")
def index():
    return FileResponse(os.path.join(BASE_DIR, "static", "index.html"))


app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")),
          name="static")


@app.on_event("startup")
def startup():
    db.get_audit_conn()
    try:
        db.get_data_conn()
    except Exception:
        pass  # configured database may be missing; settings panel will surface it
