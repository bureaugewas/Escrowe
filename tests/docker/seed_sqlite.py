"""Create a SQLite file with the same sample schema and data as the MySQL,
Postgres and SQL Server containers (tests/docker/init/*.sql). No container is
needed for SQLite; tests/test_sqlite_direct.py calls seed() itself.

    python tests/docker/seed_sqlite.py [path]     default: tests/docker/sample.sqlite
"""

from __future__ import annotations

import datetime
import random
import sqlite3
import sys
from pathlib import Path

DEFAULT_PATH = Path(__file__).parent / "sample.sqlite"

CUSTOMERS = [
    ("Acme Corp", "gold", "2021-03-01", 1),
    ("Globex", "silver", "2021-06-15", 1),
    ("Initech", "bronze", "2022-01-10", 1),
    ("Umbrella LLC", "gold", "2020-11-20", 0),
    ("Soylent Inc", "silver", "2022-08-02", 1),
    ("Hooli", "gold", "2019-05-05", 1),
    ("Vehement Capital", "bronze", "2023-02-14", 1),
    ("Massive Dynamic", "silver", "2021-09-09", 1),
    ("Stark Industries", "gold", "2018-12-01", 1),
    ("Wayne Enterprises", "gold", "2017-07-04", 1),
]
REGIONS = ["US", "EU", "APAC", "LATAM"]


def seed(path: Path | str = DEFAULT_PATH) -> Path:
    path = Path(path)
    path.unlink(missing_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE customers (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          tier TEXT NOT NULL,
          signup_date DATE NOT NULL,
          is_active BOOLEAN NOT NULL DEFAULT 1
        );
        CREATE TABLE orders (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          customer_id INTEGER NOT NULL REFERENCES customers(id),
          region TEXT NOT NULL,
          amount DECIMAL(10,2) NOT NULL,
          placed_at TIMESTAMP NOT NULL,
          is_paid BOOLEAN NOT NULL DEFAULT 0
        );
    """)
    conn.executemany("INSERT INTO customers (name, tier, signup_date, is_active) VALUES (?, ?, ?, ?)", CUSTOMERS)

    rng = random.Random(42)
    base = datetime.date(2023, 1, 1)
    orders = [
        (cid, REGIONS[n % 4], round(10 + rng.random() * 990, 2),
         str(base + datetime.timedelta(days=n)), 0 if n % 3 == 0 else 1)
        for cid in range(1, len(CUSTOMERS) + 1) for n in range(1, 7)
    ]
    conn.executemany(
        "INSERT INTO orders (customer_id, region, amount, placed_at, is_paid) VALUES (?, ?, ?, ?, ?)", orders)
    conn.commit()
    conn.close()
    return path


if __name__ == "__main__":
    out = seed(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PATH)
    print(f"wrote {out} ({len(CUSTOMERS)} customers)")
