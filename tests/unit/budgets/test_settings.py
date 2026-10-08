"""Budget defaults come from validated settings and reach the components."""

from __future__ import annotations

import pytest

from retail_analytics.application.contracts.query_compiler import AnalysisQuery
from retail_analytics.bootstrap.budgets import build_run_budgets, run_limits
from retail_analytics.bootstrap.config import ConfigError, load_backend_settings
from retail_analytics.bootstrap.query import (
    build_query_compilers,
    build_result_boundary,
)
from retail_analytics.domain.budgets import RunLimits
from tests.unit.budgets.memory_store import MemoryRunBudgetStore
from tests.unit.privacy.support import EXEC_A, SCOPE_A
from tests.unit.sql_compiler.support import view


def test_default_settings_are_the_accepted_limits() -> None:
    settings = load_backend_settings({}, env_file=None)
    assert run_limits(settings) == RunLimits()


def test_settings_override_limits_for_new_runs() -> None:
    settings = load_backend_settings(
        {
            "RETAIL_ANALYTICS_RUN_MAX_QUERIES": "4",
            "RETAIL_ANALYTICS_QUERY_MAX_BYTES": str(64 * 1024 * 1024),
            "RETAIL_ANALYTICS_RESULT_MAX_ROWS": "50",
            "RETAIL_ANALYTICS_QUERY_DEADLINE_SECONDS": "30",
        },
        env_file=None,
    )
    limits = run_limits(settings)
    assert (limits.queries, limits.bytes_per_query) == (4, 64 * 1024 * 1024)
    assert (limits.result_rows, limits.query_deadline_seconds) == (50, 30)
    budgets = build_run_budgets(settings, MemoryRunBudgetStore())
    assert budgets.default_limits == limits
    compiled = (
        build_query_compilers(settings)
        .for_executive(EXEC_A)
        .compile(
            AnalysisQuery("SELECT product_id FROM products", {}),
            catalog=view(version=SCOPE_A.entitlement_version),
            scope=SCOPE_A,
        )
    )
    assert compiled.maximum_bytes_billed == 64 * 1024 * 1024
    assert build_result_boundary(settings)._limits.max_rows == 50


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("RETAIL_ANALYTICS_RUN_MAX_QUERIES", "0"),
        ("RETAIL_ANALYTICS_MAX_TRANSIENT_ATTEMPTS", "0"),
        ("RETAIL_ANALYTICS_QUERY_DEADLINE_SECONDS", "5"),
        ("RETAIL_ANALYTICS_RESULT_MAX_BYTES", "10"),
    ],
)
def test_out_of_range_limits_are_rejected_by_name(name: str, value: str) -> None:
    with pytest.raises(ConfigError, match=name):
        load_backend_settings({name: value}, env_file=None)


def test_query_scan_limit_cannot_exceed_the_run_limit() -> None:
    with pytest.raises(ConfigError, match="QUERY_MAX_BYTES"):
        load_backend_settings(
            {
                "RETAIL_ANALYTICS_QUERY_MAX_BYTES": str(2 * 1024**3),
                "RETAIL_ANALYTICS_RUN_MAX_BYTES": str(1024**3),
            },
            env_file=None,
        )
