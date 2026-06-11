"""DuckDB connections, sample data setup, and schema introspection.

The data connection points either at the built-in sample database
(data.duckdb, auto-generated) or at any external .duckdb file selected
in the settings panel. External files are opened read-only so Escrow
never mutates a database it is governing.

audit.duckdb is always separate and never visible to agent roles.
"""

import os
import random
import threading
from datetime import date, timedelta
from typing import Optional

import duckdb

import config

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SAMPLE_DB = os.path.join(BASE_DIR, "data.duckdb")
AUDIT_DB = os.path.join(BASE_DIR, "audit.duckdb")

_data_conn = None
_data_path: Optional[str] = None
_audit_conn = None
_lock = threading.Lock()


def current_data_path() -> str:
    return config.load_config().get("database_path") or SAMPLE_DB


def _connect(path: str):
    if os.path.abspath(path) == os.path.abspath(SAMPLE_DB):
        conn = duckdb.connect(SAMPLE_DB)
        _ensure_sample_data(conn)
        return conn
    if not os.path.exists(path):
        raise FileNotFoundError(f"Database file not found: {path}")
    return duckdb.connect(path, read_only=True)


def get_data_conn() -> duckdb.DuckDBPyConnection:
    global _data_conn, _data_path
    path = current_data_path()
    with _lock:
        if _data_conn is None or _data_path != path:
            if _data_conn is not None:
                try:
                    _data_conn.close()
                except Exception:
                    pass
                _data_conn = None
            _data_conn = _connect(path)
            _data_path = path
        return _data_conn


def switch_database(path: Optional[str]) -> dict:
    """Validate and switch to a different .duckdb file (None → sample data).
    Raises on failure without losing the previous connection."""
    target = path or SAMPLE_DB
    probe = _connect(target)  # raises if unusable
    tables = [r[0] for r in probe.execute("SHOW TABLES").fetchall()]
    global _data_conn, _data_path
    with _lock:
        if _data_conn is not None and _data_conn is not probe:
            try:
                _data_conn.close()
            except Exception:
                pass
        _data_conn = probe
        _data_path = target
    config.save_config({"database_path": path})
    return {"path": target, "tables": tables}


def get_schema() -> list:
    """Tables with columns, types and row counts for the explorer."""
    conn = get_data_conn()
    out = []
    for (table,) in conn.execute("SHOW TABLES").fetchall():
        if table == "pii_registry":
            continue
        cols = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        try:
            count = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        except Exception:
            count = None
        out.append({
            "name": table,
            "row_count": count,
            "columns": [{"name": c[1], "type": c[2]} for c in cols],
        })
    return out


def schema_text() -> str:
    """Compact schema description for LLM prompts."""
    lines = []
    for t in get_schema():
        cols = ", ".join(f"{c['name']} {c['type']}" for c in t["columns"])
        lines.append(f"- {t['name']}({cols})  -- {t['row_count']} rows")
    return "\n".join(lines)


def get_audit_conn() -> duckdb.DuckDBPyConnection:
    global _audit_conn
    with _lock:
        if _audit_conn is None:
            _audit_conn = duckdb.connect(AUDIT_DB)
            _ensure_audit_schema(_audit_conn)
        return _audit_conn


def _ensure_audit_schema(conn) -> None:
    conn.execute("""
        CREATE SEQUENCE IF NOT EXISTS audit_seq;
        CREATE TABLE IF NOT EXISTS audit_log (
            id          BIGINT DEFAULT nextval('audit_seq') PRIMARY KEY,
            ts          TIMESTAMP DEFAULT current_timestamp,
            agent_role  VARCHAR,
            query_hash  VARCHAR,
            sql_text    VARCHAR,
            decision    VARCHAR,      -- allowed / denied / escalated / approved
            confidence  DOUBLE,
            decided_by  VARCHAR,      -- engine / llm / human
            reasoning   VARCHAR,
            duration_ms DOUBLE
        );
        CREATE SEQUENCE IF NOT EXISTS queue_seq;
        CREATE TABLE IF NOT EXISTS approval_queue (
            id          BIGINT DEFAULT nextval('queue_seq') PRIMARY KEY,
            ts          TIMESTAMP DEFAULT current_timestamp,
            agent_role  VARCHAR,
            sql_text    VARCHAR,
            tables_involved VARCHAR,
            llm_reasoning   VARCHAR,
            status      VARCHAR DEFAULT 'pending',  -- pending / approved / denied
            decided_ts  TIMESTAMP,
            final_sql   VARCHAR
        );
    """)


def _ensure_sample_data(conn) -> None:
    existing = {r[0] for r in conn.execute("SHOW TABLES").fetchall()}
    if {"employees", "sales_transactions", "customers", "products",
            "health_proxy", "payroll"} <= existing:
        return

    rng = random.Random(42)
    departments = ["Sales", "Engineering", "HR", "Finance", "Marketing"]
    regions = ["North", "South", "East", "West"]
    tiers = ["bronze", "silver", "gold"]
    categories = ["Hardware", "Software", "Services", "Accessories"]
    first = ["Alex", "Sam", "Jordan", "Casey", "Robin", "Taylor", "Morgan",
             "Jamie", "Riley", "Quinn"]
    last = ["Jansen", "de Vries", "Bakker", "Visser", "Smit", "Meijer",
            "Mulder", "Bos", "Vos", "Peters"]

    conn.execute("CREATE TABLE employees (id INTEGER, name VARCHAR, department VARCHAR, hire_date DATE, salary DOUBLE)")
    employees = []
    for i in range(1, 51):
        employees.append((
            i,
            f"{rng.choice(first)} {rng.choice(last)}",
            rng.choice(departments),
            date(2015, 1, 1) + timedelta(days=rng.randint(0, 3650)),
            round(rng.uniform(35000, 120000), 2),
        ))
    conn.executemany("INSERT INTO employees VALUES (?, ?, ?, ?, ?)", employees)

    conn.execute("CREATE TABLE customers (id INTEGER, name VARCHAR, region VARCHAR, account_tier VARCHAR)")
    customers = [
        (i, f"Customer {i:03d}", rng.choice(regions), rng.choice(tiers))
        for i in range(1, 201)
    ]
    conn.executemany("INSERT INTO customers VALUES (?, ?, ?, ?)", customers)

    conn.execute("CREATE TABLE products (id INTEGER, name VARCHAR, category VARCHAR, unit_price DOUBLE)")
    products = [
        (i, f"Product {i:02d}", rng.choice(categories), round(rng.uniform(5, 500), 2))
        for i in range(1, 31)
    ]
    conn.executemany("INSERT INTO products VALUES (?, ?, ?, ?)", products)

    conn.execute("CREATE TABLE sales_transactions (id INTEGER, employee_id INTEGER, customer_id INTEGER, amount DOUBLE, date DATE, product_id INTEGER)")
    sales = []
    for i in range(1, 5001):
        sales.append((
            i,
            rng.randint(1, 50),
            rng.randint(1, 200),
            round(rng.uniform(10, 2500), 2),
            date(2024, 1, 1) + timedelta(days=rng.randint(0, 700)),
            rng.randint(1, 30),
        ))
    conn.executemany("INSERT INTO sales_transactions VALUES (?, ?, ?, ?, ?, ?)", sales)

    # Intentionally small; exists only to exercise the ethics block.
    conn.execute("CREATE TABLE health_proxy (id INTEGER, customer_id INTEGER, indicator VARCHAR)")
    indicators = ["pharmacy_purchases", "gym_membership", "insurance_claims"]
    conn.executemany(
        "INSERT INTO health_proxy VALUES (?, ?, ?)",
        [(i, rng.randint(1, 200), rng.choice(indicators)) for i in range(1, 11)],
    )

    conn.execute("CREATE TABLE payroll (employee_id INTEGER, gross DOUBLE, net DOUBLE, period VARCHAR)")
    payroll = []
    for emp_id, _, _, _, salary in employees:
        gross = round(salary / 12, 2)
        payroll.append((emp_id, gross, round(gross * 0.63, 2), "2026-05"))
    conn.executemany("INSERT INTO payroll VALUES (?, ?, ?, ?)", payroll)
