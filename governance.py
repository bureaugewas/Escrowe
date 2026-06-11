"""Policy engine: parse SQL, check policies, score confidence.

Deterministic and testable independently of the LLM — this module never
calls an LLM. It returns a decision plus a confidence score; the caller
(main.py) escalates to the LLM reviewer and the human queue when the
confidence is too low.
"""

import os
from dataclasses import dataclass, field
from typing import Optional

import yaml
from sqlglot import exp, parse_one
from sqlglot.errors import ParseError

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
POLICY_DIR = os.path.join(BASE_DIR, "policies")

# Confidence thresholds from the spec
LLM_THRESHOLD = 0.7     # below this → ask the LLM reviewer
HUMAN_THRESHOLD = 0.5   # below this after LLM review → human queue


def load_policies() -> dict:
    with open(os.path.join(POLICY_DIR, "agent_policies.yaml")) as f:
        agents = yaml.safe_load(f)
    with open(os.path.join(POLICY_DIR, "ethics_rules.yaml")) as f:
        ethics = yaml.safe_load(f)
    return {"roles": agents.get("roles", {}),
            "ethics": ethics.get("prohibited_combinations", [])}


@dataclass
class ParsedQuery:
    tables: list = field(default_factory=list)
    joins: list = field(default_factory=list)
    aggregations: list = field(default_factory=list)
    row_columns: list = field(default_factory=list)     # non-aggregated SELECT columns
    group_by: list = field(default_factory=list)
    has_subquery: bool = False
    is_select: bool = True
    error: Optional[str] = None


@dataclass
class Decision:
    decision: str               # allow / deny / escalate_human
    confidence: float
    reasoning: list
    triggered_rules: list
    suggested_rewrite: Optional[str] = None
    parsed: Optional[ParsedQuery] = None


def parse_sql(sql: str) -> ParsedQuery:
    pq = ParsedQuery()
    try:
        tree = parse_one(sql, read="duckdb")
    except ParseError as e:
        pq.error = f"SQL could not be parsed: {e}"
        return pq

    pq.is_select = isinstance(tree, (exp.Select, exp.Union, exp.Subquery))
    pq.tables = sorted({t.name.lower() for t in tree.find_all(exp.Table)})
    pq.joins = [j.sql() for j in tree.find_all(exp.Join)]
    pq.aggregations = sorted({a.sql_name().upper() for a in tree.find_all(exp.AggFunc)})
    pq.has_subquery = any(tree.find_all(exp.Subquery)) or any(tree.find_all(exp.CTE)) or \
        len(list(tree.find_all(exp.Select))) > 1

    select = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
    if select:
        pq.group_by = [g.sql().lower() for g in
                       (select.args.get("group").expressions if select.args.get("group") else [])]
        row_cols = set()
        for proj in select.expressions:
            if isinstance(proj, exp.Star):
                row_cols.add("*")
                continue
            for col in proj.find_all(exp.Column):
                if col.find_ancestor(exp.AggFunc) is not None:
                    continue
                name = col.sql().lower()
                if name not in pq.group_by and col.name.lower() not in pq.group_by:
                    row_cols.add(col.name.lower())
            if isinstance(proj, exp.Column) is False and proj.find(exp.Star) is not None \
                    and proj.find(exp.AggFunc) is None:
                row_cols.add("*")
        pq.row_columns = sorted(row_cols)
    return pq


def _suggest_rewrite(table: str, row_columns: list) -> str:
    """Build a safe aggregated alternative for a row-level query."""
    aggs = ["COUNT(*) AS n_rows"]
    if "amount" in row_columns:
        aggs.append("AVG(amount) AS avg_amount")
    group_candidates = [c for c in row_columns
                        if c not in ("*", "amount", "id") and not c.endswith("_id")]
    group_by = " GROUP BY {}".format(group_candidates[0]) if group_candidates else ""
    if group_by:
        aggs.insert(0, group_candidates[0])
    return "SELECT {} FROM {}{}".format(", ".join(aggs), table, group_by)


def evaluate_query(sql: str, agent_role: str,
                   policies: Optional[dict] = None) -> Decision:
    policies = policies or load_policies()
    roles = policies["roles"]

    if agent_role not in roles:
        return Decision("deny", 1.0, [f"Unknown agent role '{agent_role}'."],
                        ["unknown_role"])

    role = roles[agent_role]
    role_tables = role.get("tables", {}) or {}
    default_access = role.get("default_table_access")
    pq = parse_sql(sql)

    if pq.error:
        return Decision("deny", 0.35, [pq.error,
                        "Unparseable queries cannot be verified against policy."],
                        ["parse_error"], parsed=pq)

    if not pq.is_select:
        reasons = ["Only SELECT statements are permitted through the governance layer."]
        if role.get("read_only"):
            reasons.append(f"Role '{agent_role}' is explicitly read-only.")
        return Decision("deny", 1.0, reasons, ["non_select_statement"], parsed=pq)

    reasoning: list = []
    triggered: list = []
    confidence = 1.0

    # 2. Ethics: prohibited table combinations apply to every role
    query_tables = set(pq.tables)
    for combo in policies["ethics"]:
        combo_tables = {t.lower() for t in combo["tables"]}
        if combo_tables <= query_tables:
            triggered.append("ethics:" + "+".join(sorted(combo_tables)))
            if combo.get("escalate_instead_of_block"):
                reasoning.append(
                    f"Ethics rule: {combo['reason']} — escalated to human approval.")
                return Decision("escalate_human", 0.4, reasoning, triggered, parsed=pq)
            reasoning.append(f"Ethics rule: {combo['reason']}")
            return Decision("deny", 1.0, reasoning, triggered, parsed=pq)

    # 3. Table access for this role → build effective access rules
    prohibited = {t.lower() for t in role.get("prohibited_tables", []) or []}
    known_tables = {t.lower() for t in _all_known_tables(policies)}
    effective_rules: dict = {}
    for table in pq.tables:
        if table in prohibited:
            triggered.append(f"prohibited_table:{table}")
            reasoning.append(f"Table '{table}' is explicitly prohibited for role "
                             f"'{agent_role}'.")
            return Decision("deny", 1.0, reasoning, triggered, parsed=pq)
        if table in role_tables:
            effective_rules[table] = role_tables[table] or {}
        elif default_access:
            effective_rules[table] = {"access": default_access}
        else:
            triggered.append(f"table_not_allowed:{table}")
            reasoning.append(f"Table '{table}' is not in the allowed tables for role "
                             f"'{agent_role}'.")
            # Policy clearly covers known tables; unknown tables are ambiguous.
            conf = 0.95 if table in known_tables else 0.35
            if table not in known_tables:
                reasoning.append(f"Table '{table}' is not referenced anywhere in the "
                                 "policy files — the policy does not clearly cover "
                                 "this case.")
            return Decision("deny", conf, reasoning, triggered, parsed=pq)

    # 4. Aggregation requirements
    suggested_rewrite = None
    for table, rule in effective_rules.items():
        if rule.get("access") != "aggregated_only":
            continue
        if pq.row_columns:
            triggered.append(f"aggregated_only:{table}")
            reasoning.append(
                f"Role '{agent_role}' may only see aggregated results from "
                f"'{table}', but the query selects row-level columns: "
                f"{', '.join(pq.row_columns)}.")
            suggested_rewrite = _suggest_rewrite(table, pq.row_columns)
            return Decision("deny", 0.95, reasoning, triggered,
                            suggested_rewrite=suggested_rewrite, parsed=pq)
        if not pq.aggregations:
            triggered.append(f"aggregated_only:{table}")
            reasoning.append(f"'{table}' requires aggregated access but the query "
                             "uses no aggregate functions.")
            return Decision("deny", 0.9, reasoning, triggered, parsed=pq)
        allowed_aggs = {a.upper() for a in rule.get("allowed_aggregations", []) or []}
        if allowed_aggs:
            illegal = set(pq.aggregations) - allowed_aggs
            if illegal:
                triggered.append(f"aggregation_not_allowed:{table}")
                reasoning.append(f"Aggregations {sorted(illegal)} are not permitted "
                                 f"on '{table}' (allowed: {sorted(allowed_aggs)}).")
                return Decision("deny", 0.85, reasoning, triggered, parsed=pq)
        if rule.get("group_by_required") and not pq.group_by:
            triggered.append(f"group_by_required:{table}")
            reasoning.append(f"Policy requires GROUP BY for aggregated queries on "
                             f"'{table}', but the query has none.")
            suggested_rewrite = _suggest_rewrite(table, ["amount", "product_id"])
            return Decision("deny", 0.8, reasoning, triggered,
                            suggested_rewrite=suggested_rewrite, parsed=pq)
        # Grouping by an identifier still isolates individuals → ambiguous
        id_groups = [g for g in pq.group_by if g == "id" or g.endswith("_id")
                     or g.endswith(".id")]
        if id_groups:
            triggered.append(f"group_by_identifier:{table}")
            reasoning.append(
                f"Query groups '{table}' by identifier column(s) "
                f"{id_groups} — aggregates per individual may still expose "
                "row-level information. Policy does not clearly cover this.")
            confidence = min(confidence, 0.55)

    # 5. Prohibited joins for this role
    for pair in role.get("prohibited_joins", []) or []:
        pair_set = {t.lower() for t in pair}
        if pair_set <= query_tables:
            triggered.append("prohibited_join:" + "+".join(sorted(pair_set)))
            if pq.joins:
                reasoning.append(f"Role '{agent_role}' may not join "
                                 f"{sorted(pair_set)}.")
                return Decision("deny", 0.95, reasoning, triggered, parsed=pq)
            reasoning.append(
                f"Query references both {sorted(pair_set)} without an explicit "
                "JOIN — possible indirect correlation, policy does not clearly "
                "cover this.")
            confidence = min(confidence, 0.4)

    # 6. Unusual constructs lower confidence
    if pq.has_subquery:
        triggered.append("subquery_present")
        reasoning.append("Query contains subqueries/CTEs; static analysis of "
                         "indirect data flows is incomplete.")
        confidence = min(confidence, 0.6)

    if confidence >= LLM_THRESHOLD:
        reasoning.append("Query complies with all policy rules for this role.")
    return Decision("allow", confidence, reasoning, triggered,
                    suggested_rewrite=suggested_rewrite, parsed=pq)


def _all_known_tables(policies: dict) -> set:
    known = set()
    for role in policies["roles"].values():
        known |= set((role.get("tables") or {}).keys())
        known |= {t for t in role.get("prohibited_tables", []) or []}
        for pair in role.get("prohibited_joins", []) or []:
            known |= set(pair)
    for combo in policies["ethics"]:
        known |= set(combo["tables"])
    return known


def policy_excerpt_for(agent_role: str, tables: list,
                       policies: Optional[dict] = None) -> str:
    """Policy context string for LLM prompts (reviewer and SQL generator)."""
    policies = policies or load_policies()
    role = policies["roles"].get(agent_role, {})
    relevant_ethics = [c for c in policies["ethics"]
                       if not tables or
                       {t.lower() for t in c["tables"]} & set(tables)]
    return yaml.safe_dump({"role_policy": {agent_role: role},
                           "relevant_ethics_rules": relevant_ethics},
                          sort_keys=False)
