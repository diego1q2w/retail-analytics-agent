# ruff: noqa: S608
# Query text is built from constant test fragments for the restricted compiler.
"""Live BigQuery dry runs of the SQL in the scripted agent plans (free; no rows).

The offline evaluation warehouse is DuckDB, which resolves names differently
from BigQuery. These tests compile every ``execute_analysis`` step of the
scripted plans (and a deterministic sample of property-style queries) with the
real keyed derivations, and have BigQuery validate exactly that text. Steps the
compiler rejects on purpose (adversarial plans) must stay rejected.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path

import pytest
from google.cloud import bigquery

from retail_analytics.adapters.sql_compiler import (
    ReferenceKeyring,
    ScopedSqlglotCompilers,
)
from retail_analytics.application.contracts.query_compiler import (
    AnalysisQuery,
    CompiledQuery,
)
from retail_analytics.application.query_compiler import QueryRejected
from tests.live.test_sql_compiler_dry_run import LIVE_SCOPE, _dry_run, client
from tests.unit.sql_compiler.support import DATASET, view

pytestmark = pytest.mark.live

__all__ = ["client"]

SCRIPTS = Path(__file__).resolve().parents[2] / "evaluation" / "agent-scripts"
_COMPILER = ScopedSqlglotCompilers(
    DATASET, ReferenceKeyring(secrets.token_bytes(32))
).for_executive("dry-run")


def script_queries() -> list[tuple[str, str]]:
    cases: list[tuple[str, str]] = []
    for path in sorted(SCRIPTS.glob("*.json")):
        plans = json.loads(path.read_text(encoding="utf-8"))["plans"]
        for number, steps in enumerate(plans.values()):
            for index, step in enumerate(steps):
                if step.get("call") == "execute_analysis":
                    cases.append((f"{path.stem}-{number}-{index}", step["args"]["sql"]))
    return cases


_DIMENSIONS = ["s.product_id", "s.item_status", "p.category", "c.state", "c.age_band"]
_MEASURES = (
    "SUM(s.sale_amount) AS m0, COUNT(DISTINCT s.order_ref) AS m1, "
    "SAFE_DIVIDE(SUM(s.sale_amount), COUNT(*)) AS m2"
)
_JOINS = (
    "FROM sales_items s JOIN products p ON s.product_id = p.product_id "
    "LEFT JOIN customers c ON s.customer_ref = c.customer_ref"
)


def property_style_queries() -> list[tuple[str, str]]:
    cases: list[tuple[str, str]] = []
    for dimension in _DIMENSIONS:
        base = f"SELECT {dimension} AS d, {_MEASURES} {_JOINS} GROUP BY d"
        cases.append((f"{dimension}-alias", base))
        cases.append((f"{dimension}-ordered", base + " ORDER BY m0 DESC"))
        cases.append((f"{dimension}-cte", f"WITH q AS ({base}) SELECT d, m0 FROM q"))
        cases.append(
            (
                f"{dimension}-expr-ordered",
                f"SELECT {dimension} AS d, {_MEASURES} {_JOINS} "
                f"GROUP BY {dimension} ORDER BY d",
            )
        )
    return cases


def _compile(sql: str) -> CompiledQuery:
    return _COMPILER.compile(
        AnalysisQuery(sql, {}),
        catalog=view(version=LIVE_SCOPE.entitlement_version),
        scope=LIVE_SCOPE,
    )


@pytest.mark.parametrize(("name", "sql"), script_queries(), ids=lambda v: v[:30])
def test_scripted_plan_sql_passes_bigquery_dry_run(
    client: bigquery.Client, name: str, sql: str
) -> None:
    try:
        compiled = _compile(sql)
    except QueryRejected:
        pytest.skip("deliberately rejected by the compiler (adversarial step)")
    assert 0 < _dry_run(client, compiled) <= compiled.maximum_bytes_billed


@pytest.mark.parametrize(
    ("name", "sql"), property_style_queries(), ids=lambda v: v[:40]
)
def test_property_style_sql_passes_bigquery_dry_run(
    client: bigquery.Client, name: str, sql: str
) -> None:
    compiled = _compile(sql)
    assert 0 < _dry_run(client, compiled) <= compiled.maximum_bytes_billed
