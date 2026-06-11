# Escrow — Local AI Data Governance PoC

## Vision

Escrow is a local-first AI governance layer that sits between AI agents and a DuckDB
database. It enforces data access policies at query time: which agent type can see which
data, whether results must be aggregated before exposure, and what to do when the policy
engine is uncertain. It runs entirely on localhost, requires no cloud services, and is
designed as a proof of concept for a future COSS product.

The core thesis: as local LLMs become capable enough for most analytics tasks, the
competitive advantage shifts from the model to the governance infrastructure around it.
Escrow proves that governance-enforced local AI analytics is practical today.

---

## What this PoC demonstrates

1. **Agent-type access control** — different agent roles get different data access, defined
   in a policy file. Not user-based: the *agent identity* determines access.

2. **Aggregation enforcement** — some roles may see aggregated results (COUNT, AVG, SUM
   grouped by category) but never row-level records. The system parses the SQL the agent
   generates and determines whether the result exposes individual records or not.

3. **Ethics policy** — certain data combinations are prohibited regardless of individual
   table access. Example: joining `customers` with `health_indicators` is blocked for any
   agent type, because company policy forbids that inference.

4. **Escalation chain** — when the policy engine cannot confidently allow or deny a
   query, it escalates. First to a second LLM acting as a policy reviewer. If still
   uncertain, it queues the request for a human approver in the UI.

5. **Audit log** — every agent query, every policy decision (allowed / denied / rewritten
   / escalated), and every human approval is logged with reasoning.

---

## Architecture: localhost web interface

Build this as a **localhost web application** (not a DuckDB extension). Reason: a web UI
can demonstrate the governance layer visually, show the escalation queue, display the
audit log, and let a human approve/deny borderline queries — all in one interface. DuckDB
is the engine underneath.

```
┌─────────────────────────────────────────────────────┐
│                  Browser (localhost)                 │
│  ┌──────────────┐  ┌───────────────┐  ┌──────────┐  │
│  │ Query panel  │  │ Approval queue│  │Audit log │  │
│  └──────┬───────┘  └───────┬───────┘  └──────────┘  │
└─────────┼──────────────────┼──────────────────────── ┘
          │                  │ human decision
┌─────────▼──────────────────▼──────────────────────── ┐
│                 FastAPI backend (Python)               │
│                                                        │
│  ┌──────────────────────────────────────────────────┐ │
│  │              Governance Engine                    │ │
│  │  1. Parse SQL with SQLGlot                        │ │
│  │  2. Load agent policy from YAML                   │ │
│  │  3. Check table access, prohibited joins          │ │
│  │  4. Check aggregation requirement                 │ │
│  │  5. Confidence score → allow / deny / escalate    │ │
│  └─────────────────┬────────────────────────────────┘ │
│                    │ if uncertain                       │
│  ┌─────────────────▼────────────────────────────────┐ │
│  │         Policy Reviewer LLM (Ollama)              │ │
│  │  Receives: query + agent role + policy context    │ │
│  │  Returns: allow/deny + reasoning                  │ │
│  │  If still uncertain → human queue                 │ │
│  └─────────────────┬────────────────────────────────┘ │
│                    │ if allowed                         │
│  ┌─────────────────▼────────────────────────────────┐ │
│  │                  DuckDB                           │ │
│  │  Sample dataset: employees, sales, health_proxy   │ │
│  └──────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────── ┘
```

---

## Tech stack

| Layer | Choice | Reason |
|---|---|---|
| Backend | Python + FastAPI | Fast to build, async, easy DuckDB integration |
| Database | DuckDB (in-process) | The engine we're governing |
| SQL parser | SQLGlot | Parse and analyse queries without executing them |
| Local LLM | Ollama HTTP API (llama3 or similar) | Local inference, no data leaves |
| Frontend | Vanilla HTML + JS (single file) | No build step, runs immediately |
| Policy config | YAML files | Human-readable, version-controllable |
| Audit store | DuckDB table (separate db file) | Eat our own cooking |

If Ollama is not available, the LLM reviewer falls back to a mock that always returns
`uncertain` so the human queue gets exercised.

---

## File structure

```
Escrow/
├── main.py                  # FastAPI app, all routes
├── governance.py            # Policy engine: parse, check, score, escalate
├── db.py                    # DuckDB connection + sample data setup
├── llm.py                   # Ollama client + mock fallback
├── policies/
│   ├── agent_policies.yaml  # Roles, table access, aggregation rules
│   └── ethics_rules.yaml    # Prohibited joins and inferences
├── static/
│   └── index.html           # Single-page UI
└── audit.duckdb             # Audit log database (auto-created)
```

---

## Policy schema

### `agent_policies.yaml`

```yaml
roles:

  sales_analyst:
    description: "Can analyse sales performance, no individual customer records"
    tables:
      sales_transactions:
        access: aggregated_only   # never row-level
        allowed_aggregations: [SUM, AVG, COUNT, MAX, MIN]
        group_by_required: true
      products:
        access: full
      customers:
        access: aggregated_only
    max_result_rows: 100

  hr_assistant:
    description: "Can access HR data, never correlated with performance metrics"
    tables:
      employees:
        access: full
      payroll:
        access: aggregated_only
    prohibited_joins:
      - [employees, sales_transactions]  # cannot correlate people with performance

  finance_auditor:
    description: "Read-only access to financial tables, full aggregation allowed"
    tables:
      sales_transactions:
        access: full
      payroll:
        access: full
    read_only: true

  anonymous_demo:
    description: "Public demo role, aggregations only, no sensitive tables"
    tables:
      sales_transactions:
        access: aggregated_only
    prohibited_tables: [employees, payroll, customers]
```

### `ethics_rules.yaml`

```yaml
prohibited_combinations:
  - tables: [customers, health_proxy]
    reason: "Company policy prohibits inferring health status from customer data"
    escalate_instead_of_block: false

  - tables: [employees, location_data]
    reason: "Employee location tracking is prohibited under works council agreement"
    escalate_instead_of_block: false

  - tables: [sales_transactions, employees]
    reason: "Correlating employee identity with sales performance requires HR approval"
    escalate_instead_of_block: true   # goes to human queue, not auto-blocked
```

---

## Governance engine logic

```
function evaluate_query(sql, agent_role):

  1. Parse SQL with SQLGlot → extract tables, joins, aggregations, WHERE clauses

  2. Check prohibited table combinations (ethics_rules.yaml)
     → if match and escalate=false: DENY immediately, log reason
     → if match and escalate=true: add to human queue

  3. Check table access for this role
     → if table not in role's allowed tables: DENY

  4. Check aggregation requirement
     → if table requires aggregated_only:
         → check that query has GROUP BY and only aggregate functions in SELECT
         → if row-level columns present in SELECT: DENY or REWRITE
         → if result_rows > max_result_rows: DENY

  5. Check prohibited joins for this role
     → if join between prohibited table pair: DENY

  6. Compute confidence score (0.0 – 1.0)
     → 1.0 = clean allow or clean deny, no ambiguity
     → < 0.7 = policy covers the case but query is unusual
     → < 0.4 = policy doesn't clearly cover this case

  7. If confidence < 0.7:
     → call policy_reviewer_llm(sql, agent_role, policy_context, current_decision)
     → LLM returns: decision + reasoning + new_confidence

  8. If confidence still < 0.5 after LLM review:
     → add to human_approval_queue
     → return PENDING to caller

  9. Log everything to audit.duckdb

  10. If ALLOW: execute query on DuckDB, return results
```

---

## LLM policy reviewer prompt

When escalating to the LLM reviewer, send this structured prompt:

```
You are a data governance policy reviewer. An AI agent has submitted a SQL query.
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

INITIAL AUTOMATED DECISION: {allow/deny} (confidence: {score})
REASON FOR ESCALATION: {reason}

Respond in JSON only:
{
  "decision": "allow" | "deny" | "uncertain",
  "reasoning": "one sentence explanation",
  "confidence": 0.0-1.0,
  "suggested_rewrite": "optional safer SQL if applicable"
}
```

---

## Sample dataset

Auto-generate on startup if not present:

```python
# employees: 50 rows — id, name, department, hire_date, salary
# sales_transactions: 5000 rows — id, employee_id, customer_id, amount, date, product_id
# customers: 200 rows — id, name, region, account_tier
# products: 30 rows — id, name, category, unit_price
# health_proxy: 10 rows — intentionally small, exists only to test ethics blocking
# payroll: 50 rows — employee_id, gross, net, period
```

---

## UI: four panels

**1. Query panel** (left, main area)
- Dropdown: select agent role
- Text area: natural language query OR raw SQL
- If natural language: call Ollama to generate SQL first (shows the generated SQL before
  submission, user can edit)
- Submit button
- Result area: shows table or "blocked" message with reason

**2. Policy inspector** (right sidebar, collapsible)
- Shows the active policy for the selected role
- Highlights which rules were triggered by the last query
- Read-only in the PoC

**3. Human approval queue** (top bar, badge with count)
- Lists pending queries waiting for human decision
- Shows: agent role, submitted SQL, LLM reviewer reasoning, tables involved
- Approve / Deny buttons
- Optional: rewrite SQL before approving

**4. Audit log** (bottom panel, toggleable)
- Table: timestamp, agent role, query hash, decision, confidence, who decided (engine /
  LLM / human), duration
- Filter by role, decision, date

---

## Demo flows to exercise in order

1. **Clean allow** — `finance_auditor` queries `SELECT SUM(amount) FROM sales_transactions`
   → straight pass, high confidence.

2. **Aggregation enforcement** — `sales_analyst` queries
   `SELECT customer_id, amount FROM sales_transactions LIMIT 10`
   → blocked because row-level customer data is not permitted for this role.

3. **Aggregation rewrite** — same agent queries
   `SELECT customer_id, amount FROM sales_transactions`
   → governance engine suggests rewrite:
   `SELECT COUNT(*), AVG(amount) FROM sales_transactions GROUP BY region`

4. **Ethics block** — any role queries a JOIN between `customers` and `health_proxy`
   → hard block regardless of role, reason logged.

5. **Escalation to LLM** — `hr_assistant` queries something ambiguous like a subquery
   that indirectly correlates employees and sales without an explicit JOIN
   → low confidence → LLM reviewer called → LLM denies with reasoning.

6. **Escalation to human** — a query involving `sales_transactions` and `employees`
   (which has `escalate_instead_of_block: true`)
   → appears in approval queue → human approves or denies in UI.

7. **Audit review** — show the full audit trail of all the above decisions.

---

## Out of scope for this PoC

- Authentication (single user, localhost only)
- Multiple concurrent agents
- Fine-tuning the policy reviewer LLM
- Production hardening, rate limiting
- DuckDB extension packaging (future step after PoC validates the concept)

---

## Success criteria

The PoC is successful if a non-technical observer can:
- Understand immediately why certain queries are blocked
- See the escalation chain work end-to-end (engine → LLM → human)
- Read the audit log and reconstruct what every agent did and why
- Change a policy rule in the YAML and see it enforced on the next query

---

## Notes for the AI building this

- Use `sqlglot.parse_one(sql)` and walk the AST to extract tables and check for
  aggregations. Do not use regex on SQL strings.
- The governance engine should be deterministic and testable independently of the LLM.
  The LLM is only called when the rules engine produces low confidence.
- Keep the frontend as a single `index.html` with inline JS — no npm, no build step.
  Use `fetch()` to call the FastAPI backend.
- DuckDB opens two separate database files: `data.duckdb` for the sample data,
  `audit.duckdb` for the audit log. Never query audit tables from agent roles.
- If Ollama is unreachable, the `llm.py` module returns a mock response that marks
  confidence as 0.3 so the human queue always gets exercised in demo mode.
- Start with `uvicorn main:app --reload --port 8765`
