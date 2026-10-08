"""Live BigQuery checks of the production privacy derivations.

- Dry runs (free, no rows read) of customer and demographic queries compiled
  with the real keyed references and age bands against the public tables.
- One tiny query over literals only (no table, no customer data) proving that
  BigQuery computes exactly the HMAC reference and grid band the application
  expects.

Uses a test-only key. Skipped without a configured project and credentials.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from google.cloud import bigquery
from sqlglot import exp

from retail_analytics.adapters.google_access import create_bigquery_client
from retail_analytics.adapters.sql_compiler import (
    KeyedDerivations,
    ReferenceKeyring,
    ScopedSqlglotCompilers,
)
from retail_analytics.application.query_compiler import (
    AnalysisQuery,
    CompiledQuery,
    QueryParameter,
    ScalarValue,
)
from retail_analytics.bootstrap.config import load_backend_settings
from retail_analytics.domain.privacy import age_band_label
from tests.unit.sql_compiler.support import DATASET, scope, view

pytestmark = pytest.mark.live
ROOT = Path(__file__).resolve().parents[2]
KEYRING = ReferenceKeyring(b"live-test-only-reference-master-key-000000")
COMPILERS = ScopedSqlglotCompilers(DATASET, KEYRING)
LIVE_SCOPE = scope(*range(1, 2001))
SAMPLE_REF = KEYRING.for_executive("demo-a").reference("customer_ref", 1)

QUERIES: list[tuple[str, dict[str, ScalarValue]]] = [
    (
        "SELECT s.customer_ref AS customer, c.state AS region, c.age_band AS band, "
        "SUM(s.sale_amount) AS completed_sales FROM sales_items s "
        "JOIN customers c ON s.customer_ref = c.customer_ref "
        "WHERE s.item_status = 'Complete' GROUP BY customer, region, band "
        "ORDER BY completed_sales DESC LIMIT 10",
        {},
    ),
    (
        "SELECT country, state, age_band, COUNT(DISTINCT customer_ref) AS n "
        "FROM customers GROUP BY country, state, age_band",
        {},
    ),
    (
        "SELECT s.order_ref, s.item_ref, s.sale_amount FROM sales_items s "
        "WHERE s.customer_ref = @customer",
        {"customer": SAMPLE_REF},
    ),
    (
        "SELECT o.order_ref, o.visible_item_count, c.age_band FROM orders o "
        "JOIN customers c ON o.customer_ref = c.customer_ref "
        "WHERE o.customer_ref = @customer",
        {"customer": SAMPLE_REF},
    ),
    (
        "SELECT CASE WHEN age_band IN ('20-24', '25-29') THEN '20-29' "
        "ELSE 'other' END AS age_group, COUNT(*) AS n FROM customers GROUP BY 1",
        {},
    ),
]


def _parameter(
    p: QueryParameter,
) -> bigquery.ScalarQueryParameter | bigquery.ArrayQueryParameter:
    if isinstance(p.value, tuple):
        return bigquery.ArrayQueryParameter(p.name, p.type.value, list(p.value))
    return bigquery.ScalarQueryParameter(p.name, p.type.value, p.value)


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


def _compile(sql: str, values: dict[str, ScalarValue]) -> CompiledQuery:
    return COMPILERS.for_executive("demo-a").compile(
        AnalysisQuery(sql, values), catalog=view(), scope=LIVE_SCOPE
    )


@pytest.mark.parametrize(("sql", "values"), QUERIES)
def test_privacy_queries_dry_run(
    client: bigquery.Client, sql: str, values: dict[str, ScalarValue]
) -> None:
    compiled = _compile(sql, values)
    config = bigquery.QueryJobConfig(
        dry_run=True,
        use_query_cache=False,
        query_parameters=[_parameter(p) for p in compiled.parameters],
        maximum_bytes_billed=compiled.maximum_bytes_billed,
    )
    job = client.query(compiled.sql, job_config=config)
    assert job.total_bytes_processed is not None
    assert job.total_bytes_processed <= compiled.maximum_bytes_billed


def test_bigquery_computes_the_expected_reference_and_band(
    client: bigquery.Client,
) -> None:
    key = KEYRING.for_executive("demo-a")
    derivations = KeyedDerivations(key)
    reference = derivations.opaque_reference("customer_ref", exp.Literal.number(42))
    bands = [
        exp.alias_(derivations.age_band(exp.Literal.number(age)), f"b{age}")
        for age in (12, 27, 89, 95)
    ]
    select = exp.select(exp.alias_(reference, "r"), *bands)
    config = bigquery.QueryJobConfig(
        use_query_cache=False,
        query_parameters=[_parameter(p) for p in derivations.parameters()],
        maximum_bytes_billed=10 * 1024**2,
    )
    rows = list(client.query(select.sql(dialect="bigquery"), job_config=config))
    assert len(rows) == 1
    row = rows[0]
    assert row["r"] == key.reference("customer_ref", 42)
    for age in (12, 27, 89, 95):
        assert row[f"b{age}"] == age_band_label(age)
