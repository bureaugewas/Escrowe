"""PII registry: which columns hold GDPR-sensitive data, and masking helpers.

Two sources, merged:
  1. policies/pii_schema.yaml
  2. an optional `pii_registry(table_name, column_name, category)` table
     inside the connected DuckDB file itself
"""

import os
from typing import Optional

import yaml

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PII_YAML = os.path.join(BASE_DIR, "policies", "pii_schema.yaml")


def load_registry(conn=None) -> dict:
    """Returns {table: {column: category}} with lowercase names."""
    registry: dict = {}
    if os.path.exists(PII_YAML):
        with open(PII_YAML) as f:
            data = yaml.safe_load(f) or {}
        for table, cols in (data.get("pii_columns") or {}).items():
            registry[table.lower()] = {c.lower(): str(cat)
                                       for c, cat in (cols or {}).items()}
    if conn is not None:
        try:
            rows = conn.execute(
                "SELECT table_name, column_name, category FROM pii_registry"
            ).fetchall()
            for table, col, cat in rows:
                registry.setdefault(str(table).lower(), {})[str(col).lower()] = str(cat)
        except Exception:
            pass  # no pii_registry table in this database
    return registry


def mask_value(value):
    if value is None:
        return None
    s = str(value)
    if s and s[0].isalpha():
        return s[0] + "•••"
    return "•••"


def column_categories(registry: dict) -> dict:
    """Flatten to {column_name: category} across all tables (best effort,
    used to mask query results where table origin is unknown)."""
    flat: dict = {}
    for cols in registry.values():
        flat.update(cols)
    return flat


def mask_result(columns: list, rows: Optional[list], registry: dict):
    """Mask PII columns in a result set by column-name match.
    Returns (rows, masked_column_names)."""
    if rows is None:
        return rows, []
    flat = column_categories(registry)
    idxs = [i for i, c in enumerate(columns) if str(c).lower() in flat]
    if not idxs:
        return rows, []
    masked = [[mask_value(v) if i in idxs else v for i, v in enumerate(row)]
              for row in rows]
    return masked, [columns[i] for i in idxs]
