"""What the agent is allowed to know about the data: names, types, comments,
approximate sizes. Never a value.

**This module never reads data.** Everything here comes from the database's
own system catalog (e.g. MySQL's information_schema), read through the
engine. No SELECT is ever issued against a user table, so no value from the
data can reach the agent - a database often holds tables of API keys, tokens
or personal records, and a "representative sample" of those is a leak.
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
        return {"table": self.fqn, "comment": self.comment,
                "approx_rows": self.approx_rows, "columns": [c.to_dict() for c in self.columns]}


class Metadata:
    def __init__(self, engine: DirectEngine):
        self.engine = engine

    def all_tables(self) -> list[TableMeta]:
        sizes = self.engine.table_sizes()
        by_table: dict[str, list] = {}
        for c in self.engine.catalog():
            by_table.setdefault(c.fqn, []).append(c)

        out: list[TableMeta] = []
        for fqn in sorted(by_table):
            cols = by_table[fqn]
            tm = TableMeta(fqn=fqn, comment=cols[0].table_comment, approx_rows=sizes.get(fqn))
            tm.columns = [ColumnMeta(c.name, c.type, c.comment) for c in cols]
            out.append(tm)
        return out

    @staticmethod
    def render(tables: list[TableMeta]) -> str:
        """Compact text form for the agent prompt. Names, types, comments, sizes."""
        lines = []
        for t in tables:
            head = f"TABLE {t.fqn}"
            if t.approx_rows:                     # a scanner may report 0/unknown; don't imply "empty"
                head += f"  (~{t.approx_rows} rows)"
            lines.append(head)
            if t.comment:
                lines.append(f"  -- {t.comment}")
            for c in t.columns:
                line = f"  {c.name} {c.type}"
                if c.comment:
                    line += f"  -- {c.comment}"
                lines.append(line)
            lines.append("")
        return "\n".join(lines)
