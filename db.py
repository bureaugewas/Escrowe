"""DuckDB connections and sample data setup.

Two separate database files:
  - data.duckdb   : the sample dataset that agents query
  - audit.duckdb  : audit log + human approval queue (never visible to agent roles)
"""

import os
import random
import threading
from datetime import date, timedelta

import duckdb

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DB = os.path.join(BASE_DIR, "data.duckdb")
AUDIT_DB = os.path.join(BASE_DIR, "audit.duckdb")

_data_conn = None
_audit_conn = None
_lock = threading.Lock()


def get_data_conn() -> duckdb.DuckDBPyConnection:
    global _data_conn
    with _lock:
        if _data_conn is None:
            _data_conn = duckdb.connect(DATA_DB)
            _ensure_sample_data(_data_conn)
        return _data_conn


def get_audit_conn() -> duckdb.DuckDBPyConnection:
    global _audit_conn
    with _lock:
        if _audit_conn is None:
            _audit_conn = duckdb.connect(AUDIT_DB)
            _ensure_audit_schema(_audit_conn)
        return _audit_conn


def _ensure_audit_schema(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute("""
        CREATE SEQUENCE IF NOT EXISTS audit_seq;
        CREATE TABLE IF NOT EXISTS audit_log (
            id          BIGINT DEFAULT nextval('audit_seq') PRIMARY KEY,
            ts          TIMESTAMP DEFAULT current_timestamp,
            agent_role  VARCHAR,
            query_hash  VARCHAR,
            sql_text    VARCHAR,
            decision    VARCHAR,      -- allowed / denied / escalated / pending / approved / rejected
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


def _ensure_sample_data(conn: duckdb.DuckDBPyConnection) -> None:
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
