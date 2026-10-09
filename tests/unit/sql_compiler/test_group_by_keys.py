"""GROUP BY keys are emitted unambiguously, so no engine-specific rewrite is needed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import sqlglot
from sqlglot import exp

from retail_analytics.application.query_compiler import QueryRejected
from tests.unit.sql_compiler.support import compile_sql

_JOIN = (
    "FROM sales_items s JOIN products p ON s.product_id = p.product_id GROUP BY {key}"
)


def _keys(sql: str) -> list[list[str]]:
    tree = sqlglot.parse_one(sql, read="bigquery")
    return [
        [k.sql("bigquery") for k in s.args["group"].expressions]
        for s in tree.find_all(exp.Select)
        if s.args.get("group")
    ]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (
            "SELECT customer_ref, SUM(sale_amount) AS spend FROM sales_items "
            "GROUP BY customer_ref",
            ["`sales_items`.`customer_ref`"],
        ),
        (
            "SELECT customer_ref, SUM(sale_amount) AS spend FROM sales_items "
            "GROUP BY customer_ref ORDER BY spend DESC",
            ["1"],
        ),
        (
            "SELECT s.product_id AS product_id, p.product_name AS product_name, "
            "COUNT(*) AS n " + _JOIN.format(key="product_id, product_name"),
            ["`s`.`product_id`", "`p`.`product_name`"],
        ),
        (
            "SELECT s.product_id AS product_id, COUNT(*) AS n "
            + _JOIN.format(key="s.product_id")
            + " ORDER BY n",
            ["1"],
        ),
    ],
)
def test_group_keys_are_never_bare_alias_names(query: str, expected: list[str]) -> None:
    assert _keys(compile_sql(query).sql) == [expected]


def test_scripted_plans_emit_no_bare_group_names() -> None:
    root = Path(__file__).resolve().parents[3] / "evaluation" / "agent-scripts"
    checked = 0
    for path in sorted(root.glob("*.json")):
        for steps in json.loads(path.read_text(encoding="utf-8"))["plans"].values():
            for step in steps:
                if step.get("call") != "execute_analysis":
                    continue
                try:
                    compiled = compile_sql(step["args"]["sql"])
                except QueryRejected:
                    continue
                checked += 1
                for keys in _keys(compiled.sql):
                    assert not any(
                        isinstance(sqlglot.parse_one(k, read="bigquery"), exp.Column)
                        and "." not in k
                        for k in keys
                    ), compiled.sql
    assert checked > 30
