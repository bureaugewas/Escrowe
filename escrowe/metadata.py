"""The schema as the agent sees it: table and column names, types, comments
and approximate sizes. Never a value.

Everything here comes from the engine's catalog queries, which read only
the database's own system catalog. No SELECT is ever issued against a user
table on this path.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .engines import DirectEngine


@dataclass
class ColumnMeta:
    name: str
    type: str
    comment: str | None = None

    def to_dict(self) -> dict:
        return {"name": self.name, "type": self.type, "comment": self.comment}


@dataclass
class TableMeta:
    fqn: str
    comment: str | None = None
    columns: list[ColumnMeta] = field(default_factory=list)
    approx_rows: int | None = None

    def to_dict(self) -> dict:
        return {"table": self.fqn, "comment": self.comment, "approx_rows": self.approx_rows,
                "columns": [c.to_dict() for c in self.columns]}


def read_schema(engine: DirectEngine) -> list[TableMeta]:
    """One pass over the engine's catalog, grouped by table."""
    sizes = engine.table_sizes()
    by_table: dict[str, list] = {}
    for column in engine.catalog():
        by_table.setdefault(column.fqn, []).append(column)
    return [
        TableMeta(fqn=fqn, comment=cols[0].table_comment, approx_rows=sizes.get(fqn),
                  columns=[ColumnMeta(c.name, c.type, c.comment) for c in cols])
        for fqn, cols in sorted(by_table.items())
    ]


def render_schema(tables: list[TableMeta]) -> str:
    """Compact text for the agent prompt."""
    lines = []
    for t in tables:
        head = f"TABLE {t.fqn}"
        if t.approx_rows:                     # 0 or None may mean "unknown"; do not imply empty
            head += f"  (~{t.approx_rows} rows)"
        lines.append(head)
        if t.comment:
            lines.append(f"  -- {t.comment}")
        for c in t.columns:
            lines.append(f"  {c.name} {c.type}" + (f"  -- {c.comment}" if c.comment else ""))
        lines.append("")
    return "\n".join(lines)
