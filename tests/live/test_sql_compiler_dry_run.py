"""Live BigQuery dry runs of compiled queries (free; no rows are read).

Checks that every supported fixture query compiles to SQL BigQuery accepts,
with its typed parameters, against the real public tables, and that the
estimated scan stays within the compiled byte budget. Uses the test-only
reference/age-band derivations; it validates engine compatibility, not the
privacy design. Skipped without a configured project and credentials.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from google.cloud import bigquery

from retail_analytics.adapters.google_access import create_bigquery_client
from retail_analytics.application.contracts.query_compiler import (
    CompiledQuery,
    QueryParameter,
    ScalarValue,
)
from retail_analytics.bootstrap.config import load_backend_settings
from tests.unit.sql_compiler.support import compile_sql, scope
from tests.unit.sql_compiler.test_allowed_queries import ALLOWED

pytestmark = pytest.mark.live
ROOT = Path(__file__).resolve().parents[2]

# A realistic scope size (demo executives hold thousands of products).
LIVE_SCOPE = scope(*range(1, 2001))

EXTRA: list[tuple[str, dict[str, ScalarValue]]] = [
    (
        "SELECT SUM(sale_amount) AS t FROM sales_items WHERE ordered_date >= @start "
        "AND sale_amount >= @minimum AND sale_amount * @ratio > 0 "
        "AND product_id < @limit_id AND @flag AND item_status = @status",
        {
            "start": date(2026, 9, 1),
            "minimum": Decimal("5.5"),
            "ratio": 0.5,
            "limit_id": 100,
            "flag": True,
            "status": "Complete",
        },
    ),
    (
        "SELECT SAFE_DIVIDE(sale_amount, 2) AS half, COUNT(*) AS n FROM sales_items "
        "GROUP BY SAFE_DIVIDE(sale_amount, 2)",
        {},
    ),
]


def _parameter(
    p: QueryParameter,
) -> bigquery.ScalarQueryParameter | bigquery.ArrayQueryParameter:
    kind = p.type.value
    if isinstance(p.value, tuple):
        return bigquery.ArrayQueryParameter(p.name, kind, list(p.value))
    return bigquery.ScalarQueryParameter(p.name, kind, p.value)


@pytest.fixture(scope="module")
def client() -> bigquery.Client:
    settings = load_backend_settings(environ={}, env_file=ROOT / ".env")
    if settings.bigquery_project is None:
        pytest.skip("RETAIL_ANALYTICS_BIGQUERY_PROJECT not set")
    try:
        return create_bigquery_client(
            settings.bigquery_project, settings.bigquery_location
        )
    except Exception:  # any credential failure means "not configured"
        pytest.skip("application default credentials not configured")


def _dry_run(client: bigquery.Client, compiled: CompiledQuery) -> int:
    config = bigquery.QueryJobConfig(
        dry_run=True,
        use_query_cache=False,
        maximum_bytes_billed=compiled.maximum_bytes_billed,
        query_parameters=[_parameter(p) for p in compiled.parameters],
    )
    job = client.query(compiled.sql, job_config=config)
    return int(job.total_bytes_processed or 0)


@pytest.mark.parametrize(
    ("query", "values"),
    [*((q, {}) for q, _ in ALLOWED), *EXTRA],
)
def test_compiled_query_passes_bigquery_dry_run(
    client: bigquery.Client, query: str, values: dict[str, ScalarValue]
) -> None:
    compiled = compile_sql(query, LIVE_SCOPE, values)
    scanned = _dry_run(client, compiled)
    assert 0 <= scanned <= compiled.maximum_bytes_billed
