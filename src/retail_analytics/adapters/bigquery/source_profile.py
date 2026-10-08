# ruff: noqa: E501
"""Aggregate-only profiling of the public source tables, plus live analyses.

Everything here reads counts, ranges and sums. No query selects a name, email,
address, birth/age value per row or a raw identifier, and ``assert_sanitized``
refuses a report that contains one. Every query carries ``maximum_bytes_billed``
and its bytes processed/billed are recorded in the report.

The application analyses go through the real compiler, job adapter and result
privacy boundary, so the report carries genuine BigQuery provenance (job id,
statement fingerprint, bytes) for them.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from typing import Any

from google.cloud import bigquery

from retail_analytics.adapters.bigquery.jobs import BigQueryQueryJobs
from retail_analytics.adapters.bigquery.metadata import BigQuerySourceMetadata
from retail_analytics.adapters.sql_compiler import (
    ReferenceKeyring,
    ScopedSqlglotCompilers,
)
from retail_analytics.application.access_check import PUBLIC_DATASET
from retail_analytics.application.contracts.query_compiler import (
    AnalysisQuery,
    CompiledQuery,
)
from retail_analytics.application.contracts.warehouse_jobs import (
    JobRef,
    JobSubmission,
)
from retail_analytics.application.query_execution import query_fingerprint
from retail_analytics.application.result_privacy import ResultPrivacyBoundary
from retail_analytics.domain.access import ProductScope
from retail_analytics.domain.catalog import build_view, evaluate_health
from retail_analytics.domain.logical_catalog import default_logical_catalog

REPORT_VERSION = 1
PROFILE_BYTES_CAP = 200 * 1024 * 1024
TABLES = ("orders", "order_items", "products", "users")
# Never appear as a key or value in a stored report.
FORBIDDEN_TOKENS = (
    "first_name",
    "last_name",
    "email",
    "street_address",
    "postal_code",
    "latitude",
    "longitude",
    "user_geom",
)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")

_D = "`" + PUBLIC_DATASET + "`"


def _q(sql: str) -> str:
    """Bind the trusted dataset constant into a static statement."""
    return sql.replace("__D__", _D)


# name -> (aggregate SQL). One row each, except the grouped ones noted.
_PROFILE_SQL: Mapping[str, str] = {
    "order_items": """SELECT COUNT(*) AS rows_total, COUNT(DISTINCT id) AS distinct_id,
  COUNTIF(id IS NULL) AS null_id, COUNTIF(order_id IS NULL) AS null_order_id,
  COUNTIF(user_id IS NULL) AS null_user_id, COUNTIF(product_id IS NULL) AS null_product_id,
  COUNTIF(status IS NULL) AS null_status, COUNTIF(created_at IS NULL) AS null_created_at,
  COUNTIF(sale_price IS NULL) AS null_sale_price, COUNTIF(sale_price < 0) AS negative_sale_price,
  COUNTIF(sale_price = 0) AS zero_sale_price, MIN(sale_price) AS sale_price_min,
  MAX(sale_price) AS sale_price_max, SUM(sale_price) AS sale_price_sum,
  COUNT(DISTINCT order_id) AS distinct_orders, COUNT(DISTINCT user_id) AS distinct_users,
  COUNT(DISTINCT product_id) AS distinct_products,
  CAST(MIN(created_at) AS STRING) AS item_created_min,
  CAST(MAX(created_at) AS STRING) AS item_created_max,
  COUNTIF(created_at > CURRENT_TIMESTAMP()) AS item_created_in_future
FROM __D__.order_items""",
    "order_items_status": """SELECT status, COUNT(*) AS items, COUNTIF(sale_price IS NULL) AS null_price
FROM __D__.order_items GROUP BY status ORDER BY status""",
    "orders": """SELECT COUNT(*) AS rows_total, COUNT(DISTINCT order_id) AS distinct_order_id,
  COUNTIF(order_id IS NULL) AS null_order_id, COUNTIF(user_id IS NULL) AS null_user_id,
  COUNTIF(status IS NULL) AS null_status, COUNTIF(created_at IS NULL) AS null_created_at,
  COUNT(DISTINCT user_id) AS distinct_users,
  CAST(MIN(created_at) AS STRING) AS order_created_min,
  CAST(MAX(created_at) AS STRING) AS order_created_max,
  COUNTIF(created_at > CURRENT_TIMESTAMP()) AS order_created_in_future,
  MIN(num_of_item) AS num_of_item_min, MAX(num_of_item) AS num_of_item_max
FROM __D__.orders""",
    "orders_status": """SELECT status, COUNT(*) AS orders FROM __D__.orders
GROUP BY status ORDER BY status""",
    "orders_by_year": """SELECT EXTRACT(YEAR FROM created_at) AS year, COUNT(*) AS orders,
  COUNT(DISTINCT EXTRACT(MONTH FROM created_at)) AS months_present,
  CAST(MAX(DATE(created_at)) AS STRING) AS last_day
FROM __D__.orders GROUP BY year ORDER BY year""",
    "items_vs_orders": """SELECT COUNT(*) AS items, COUNTIF(o.order_id IS NULL) AS items_without_order,
  COUNTIF(o.order_id IS NOT NULL AND o.user_id != i.user_id) AS user_mismatch,
  COUNTIF(o.order_id IS NOT NULL AND o.status != i.status) AS status_mismatch,
  COUNTIF(o.order_id IS NOT NULL AND DATE(o.created_at) != DATE(i.created_at))
    AS item_date_differs_from_order_date
FROM __D__.order_items i LEFT JOIN __D__.orders o ON o.order_id = i.order_id""",
    "items_vs_products": """SELECT COUNT(*) AS items, COUNTIF(p.id IS NULL) AS items_without_product
FROM __D__.order_items i LEFT JOIN __D__.products p ON p.id = i.product_id""",
    "items_vs_users": """SELECT COUNT(*) AS items, COUNTIF(u.id IS NULL) AS items_without_user
FROM __D__.order_items i LEFT JOIN __D__.users u ON u.id = i.user_id""",
    "orders_vs_users": """SELECT COUNT(*) AS orders, COUNTIF(u.id IS NULL) AS orders_without_user
FROM __D__.orders o LEFT JOIN __D__.users u ON u.id = o.user_id""",
    "orders_vs_items": """SELECT COUNT(*) AS orders, COUNTIF(c IS NULL) AS orders_without_items,
  COUNTIF(c IS NOT NULL AND c != o.num_of_item) AS num_of_item_differs_from_item_rows,
  MAX(c) AS max_items_per_order
FROM __D__.orders o LEFT JOIN
  (SELECT order_id, COUNT(*) AS c FROM __D__.order_items GROUP BY order_id) x
  ON x.order_id = o.order_id""",
    "products": """SELECT COUNT(*) AS rows_total, COUNT(DISTINCT id) AS distinct_id,
  MIN(id) AS id_min, MAX(id) AS id_max, COUNTIF(name IS NULL) AS null_name,
  COUNTIF(category IS NULL) AS null_category, COUNTIF(brand IS NULL) AS null_brand,
  COUNTIF(department IS NULL) AS null_department,
  COUNTIF(retail_price IS NULL) AS null_retail_price,
  MIN(retail_price) AS retail_price_min, MAX(retail_price) AS retail_price_max,
  COUNT(DISTINCT category) AS distinct_categories, COUNT(DISTINCT brand) AS distinct_brands
FROM __D__.products""",
    "products_by_department": """SELECT department, COUNT(*) AS products, MIN(id) AS id_min,
  MAX(id) AS id_max FROM __D__.products GROUP BY department ORDER BY department""",
    "users": """SELECT COUNT(*) AS rows_total, COUNT(DISTINCT id) AS distinct_id,
  MIN(id) AS id_min, MAX(id) AS id_max, COUNTIF(age IS NULL) AS null_age,
  MIN(age) AS age_min, MAX(age) AS age_max, COUNTIF(country IS NULL) AS null_country,
  COUNTIF(state IS NULL) AS null_state, COUNT(DISTINCT country) AS distinct_countries,
  COUNT(DISTINCT state) AS distinct_states
FROM __D__.users""",
}

# Product scope of demo executive A (see the dev access provisioning).
ANALYSIS_SCOPE_PRODUCTS = tuple(range(1, 15990))
ANALYSIS_START = date(2026, 1, 1)
ANALYSIS_END = date(2026, 10, 1)

ANALYSES: Mapping[str, str] = {
    "completed_sales_by_category": (
        "SELECT p.category AS category, SUM(s.sale_amount) AS completed_item_sales, "
        "COUNT(*) AS completed_items FROM sales_items s "
        "JOIN products p ON s.product_id = p.product_id "
        "WHERE s.item_status = 'Complete' AND s.ordered_date >= @start "
        "AND s.ordered_date < @end GROUP BY category "
        "ORDER BY completed_item_sales DESC, category LIMIT 5"
    ),
    "completed_sales_by_age_band": (
        "SELECT c.age_band AS age_band, COUNT(DISTINCT s.customer_ref) AS customers, "
        "SUM(s.sale_amount) AS completed_item_sales FROM sales_items s "
        "JOIN customers c ON s.customer_ref = c.customer_ref "
        "WHERE s.item_status = 'Complete' AND s.ordered_date >= @start "
        "AND s.ordered_date < @end GROUP BY age_band ORDER BY age_band"
    ),
}

PROFILE_QUERIES: Mapping[str, str] = {k: _q(v) for k, v in _PROFILE_SQL.items()}

# The same population computed on the physical tables, to cross-check.
CROSS_CHECK_SQL = _q(
    """SELECT SUM(i.sale_price) AS completed_item_sales, COUNT(*) AS completed_items,
  COUNT(DISTINCT i.user_id) AS customers
FROM __D__.order_items i JOIN __D__.orders o ON o.order_id = i.order_id
WHERE i.status = 'Complete' AND i.product_id BETWEEN 1 AND 15989
  AND DATE(o.created_at) >= @start AND DATE(o.created_at) < @end"""
)


@dataclass(slots=True)
class QueryCost:
    name: str
    bytes_processed: int
    bytes_billed: int


def _plain(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if hasattr(value, "is_finite"):  # Decimal
        return float(value)
    return value


def _rows(job: bigquery.QueryJob) -> list[dict[str, Any]]:
    return [{k: _plain(v) for k, v in dict(r).items()} for r in job.result()]


def run_profile(client: bigquery.Client, project: str, location: str) -> dict[str, Any]:
    """Run every aggregate profile query and the metadata checks."""
    costs: list[QueryCost] = []
    results: dict[str, Any] = {}
    for name, sql in PROFILE_QUERIES.items():
        job = client.query(
            sql,
            job_config=bigquery.QueryJobConfig(
                maximum_bytes_billed=PROFILE_BYTES_CAP,
                labels={"app": "retail-analytics", "task": "t34"},
            ),
            project=project,
            location=location,
        )
        rows = _rows(job)
        costs.append(
            QueryCost(name, job.total_bytes_processed or 0, job.total_bytes_billed or 0)
        )
        results[name] = rows if len(rows) != 1 else rows[0]
        if name.endswith(("_status", "_by_year", "_by_department")):
            results[name] = rows
    metadata = _table_metadata(client)
    return {
        "tables": metadata,
        "profile": results,
        "query_costs": [asdict(c) for c in costs],
    }


def _table_metadata(client: bigquery.Client) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for table in TABLES:
        info = client.get_table(f"{PUBLIC_DATASET}.{table}")
        out[table] = {
            "num_rows": info.num_rows,
            "columns": {f.name: f"{f.field_type}/{f.mode}" for f in info.schema},
            "has_table_description": bool(info.description),
            "has_column_descriptions": any(f.description for f in info.schema),
            "labels": dict(info.labels or {}),
            "currency_named_columns": [
                f.name for f in info.schema if "currenc" in f.name.lower()
            ],
            "currency_mentioned_in_descriptions": any(
                "currenc" in (text or "").lower()
                for text in [info.description, *(f.description for f in info.schema)]
            ),
        }
    return out


async def check_mappings(project: str, location: str) -> dict[str, Any]:
    """Compare the logical catalog's source mappings with live metadata."""
    catalog = default_logical_catalog()
    schema = await BigQuerySourceMetadata(
        project, location, PUBLIC_DATASET
    ).read_schema(catalog.source_tables)
    health = evaluate_health(catalog, schema)
    return {
        "catalog_version": catalog.version,
        "source_tables": sorted(catalog.source_tables),
        "issues": [
            {"kind": i.kind.value, "relation": i.relation, "field": i.field}
            for i in health.issues
        ],
    }


async def run_analyses(
    client: bigquery.Client, project: str, location: str
) -> dict[str, Any]:
    """Compile and run application analyses with real BigQuery provenance."""
    catalog = default_logical_catalog()
    schema = await BigQuerySourceMetadata(
        project, location, PUBLIC_DATASET
    ).read_schema(catalog.source_tables)
    view = build_view(
        catalog, evaluate_health(catalog, schema), entitlement_version=1, visible=True
    )
    scope = ProductScope(frozenset(str(p) for p in ANALYSIS_SCOPE_PRODUCTS), 1)
    keyring = ReferenceKeyring(secrets.token_bytes(32))  # throwaway; refs not exported
    compiler = ScopedSqlglotCompilers(PUBLIC_DATASET, keyring).for_executive(
        "profile-t34"
    )
    jobs = BigQueryQueryJobs(project, location, client)
    boundary = ResultPrivacyBoundary()
    out: dict[str, Any] = {}
    params = {"start": ANALYSIS_START, "end": ANALYSIS_END}
    for name, sql in ANALYSES.items():
        compiled: CompiledQuery = compiler.compile(
            AnalysisQuery(sql, params), catalog=view, scope=scope
        )
        ref = JobRef(project, location, f"ra_t34_{uuid.uuid4().hex}")
        fingerprint = query_fingerprint(compiled)
        submission = JobSubmission(
            ref,
            compiled.sql,
            compiled.parameters,
            compiled.maximum_bytes_billed,
            fingerprint,
        )
        estimate = await jobs.dry_run(submission)
        await jobs.submit(submission)
        rows = await jobs.fetch_rows(ref, max_rows=200)
        snapshot = await jobs.lookup(ref)
        released = boundary.release(compiled, rows, catalog=view)
        stats = snapshot.statistics if snapshot else None
        out[name] = {
            "job_id": ref.job_id,
            "location": location,
            "statement_fingerprint": fingerprint,
            "logical_sql": compiled.logical_sql,
            "relations": sorted(compiled.relations),
            "window_start": ANALYSIS_START.isoformat(),
            "window_end_exclusive": ANALYSIS_END.isoformat(),
            "product_scope": "products 1-15989 (demo executive A)",
            "catalog_version": compiled.catalog_version,
            "dry_run_bytes": estimate,
            "bytes_processed": stats.bytes_processed if stats else None,
            "bytes_billed": stats.bytes_billed if stats else None,
            "cache_hit": stats.cache_hit if stats else None,
            "maximum_bytes_billed": compiled.maximum_bytes_billed,
            "rows": [
                {k: _plain(v) for k, v in rec.items()} for rec in released.records()
            ],
            "truncated": released.truncated,
            "masked_cells": released.masked_cells,
        }
    cross = client.query(
        CROSS_CHECK_SQL,
        job_config=bigquery.QueryJobConfig(
            maximum_bytes_billed=PROFILE_BYTES_CAP,
            query_parameters=[
                bigquery.ScalarQueryParameter("start", "DATE", ANALYSIS_START),
                bigquery.ScalarQueryParameter("end", "DATE", ANALYSIS_END),
            ],
            labels={"app": "retail-analytics", "task": "t34"},
        ),
        project=project,
        location=location,
    )
    row = _rows(cross)[0]
    bands = out["completed_sales_by_age_band"]["rows"]
    band_sales = sum(r["completed_item_sales"] for r in bands)
    band_customers = sum(r["customers"] for r in bands)
    out["physical_cross_check"] = {
        "row": row,
        "age_band_sales_matches": abs(band_sales - row["completed_item_sales"]) < 0.01,
        "age_band_customers_match": band_customers == row["customers"],
        "bytes_processed": cross.total_bytes_processed,
        "bytes_billed": cross.total_bytes_billed,
    }
    return out


def build_report(
    project: str, location: str, client: bigquery.Client
) -> dict[str, Any]:
    profile = run_profile(client, project, location)
    mappings = asyncio.run(check_mappings(project, location))
    analyses = asyncio.run(run_analyses(client, project, location))
    currency_known = any(
        t["currency_named_columns"] or t["currency_mentioned_in_descriptions"]
        for t in profile["tables"].values()
    )
    report: dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dataset": PUBLIC_DATASET,
        "location": location,
        "method": "aggregate queries only; no row-level or personal values stored",
        **profile,
        "mapping_check": mappings,
        "analyses": analyses,
        "currency": {
            "established_by_source_metadata": currency_known,
            "conclusion": "UNKNOWN"
            if not currency_known
            else "metadata mentions currency: review manually",
        },
    }
    assert_sanitized(report)
    return report


def assert_sanitized(report: object) -> None:
    """Refuse a report that mentions a personal-data column or an e-mail.

    The schema listing (``tables.*.columns``) is the only exempt part."""
    data = dict(report) if isinstance(report, Mapping) else report
    if isinstance(data, dict) and isinstance(data.get("tables"), Mapping):
        # Column names and types are schema, not data: exempt them only.
        data["tables"] = {
            name: {k: v for k, v in t.items() if k != "columns"}
            for name, t in data["tables"].items()
        }
    text = json.dumps(data).lower()
    if _EMAIL.search(text):
        raise ValueError("report is not sanitized: contains an e-mail address")
    for token in FORBIDDEN_TOKENS:
        if token in text:
            raise ValueError(f"report is not sanitized: contains {token!r}")


def render_markdown(report: Mapping[str, Any]) -> str:
    """Short human summary of a report; every number comes from the report."""
    p = report["profile"]
    items, orders = p["order_items"], p["orders"]
    lines = [
        "# Source data profile",
        "",
        f"Generated {report['generated_at']} against `{report['dataset']}` "
        f"({report['location']}). Aggregates only; no personal values.",
        "",
        "## Tables",
        "",
        "| table | rows | columns |",
        "| --- | ---: | --- |",
    ]
    for name, t in report["tables"].items():
        lines.append(f"| {name} | {t['num_rows']} | {len(t['columns'])} |")
    lines += [
        "",
        "## Findings",
        "",
        "- Item status values: "
        + ", ".join(f"{r['status']} ({r['items']})" for r in p["order_items_status"]),
        f"- Item `sale_price`: FLOAT, nulls {items['null_sale_price']}, "
        f"min {items['sale_price_min']:.2f}, max {items['sale_price_max']:.2f}, "
        f"negative {items['negative_sale_price']}, zero {items['zero_sale_price']}.",
        f"- Items: {items['rows_total']} rows, {items['distinct_id']} distinct ids "
        "(one row per item).",
        f"- Orders: {orders['rows_total']} rows, {orders['distinct_order_id']} "
        f"distinct; created {orders['order_created_min']} to "
        f"{orders['order_created_max']} UTC; future-dated orders "
        f"{orders['order_created_in_future']}.",
        f"- Item `created_at` ranges to {items['item_created_max']}; "
        f"{items['item_created_in_future']} items are future-dated (the logical "
        "`ordered_date` uses `orders.created_at`, so it is not affected).",
        f"- Items whose date differs from their order date: "
        f"{p['items_vs_orders']['item_date_differs_from_order_date']} "
        "(confirms the order date, not the item date, must be used).",
        f"- Orphans: items without order {p['items_vs_orders']['items_without_order']}, "
        f"without product {p['items_vs_products']['items_without_product']}, "
        f"without user {p['items_vs_users']['items_without_user']}; orders without "
        f"user {p['orders_vs_users']['orders_without_user']}, without items "
        f"{p['orders_vs_items']['orders_without_items']}.",
        f"- `orders.num_of_item` differs from item rows for "
        f"{p['orders_vs_items']['num_of_item_differs_from_item_rows']} orders.",
        f"- Products: {p['products']['rows_total']} rows, ids "
        f"{p['products']['id_min']}-{p['products']['id_max']}; nulls name "
        f"{p['products']['null_name']}, brand {p['products']['null_brand']}.",
        "- Departments: "
        + ", ".join(
            f"{r['department']} ids {r['id_min']}-{r['id_max']}"
            for r in p["products_by_department"]
        ),
        f"- Users: {p['users']['rows_total']} rows, ages {p['users']['age_min']}-"
        f"{p['users']['age_max']}, null age {p['users']['null_age']}, "
        f"{p['users']['distinct_countries']} countries, "
        f"{p['users']['distinct_states']} states.",
        f"- Catalog mapping issues against live metadata: "
        f"{len(report['mapping_check']['issues'])}.",
        f"- Currency: {report['currency']['conclusion']} (no currency column, "
        "label or description in the source metadata).",
        "",
        "## Analyses run through the compiler (real BigQuery jobs)",
        "",
    ]
    for name, a in report["analyses"].items():
        if name == "physical_cross_check":
            lines.append(
                f"- Physical cross-check of the same population: {a['row']} "
                f"({a['bytes_billed']} bytes billed); age-band sales match "
                f"{a['age_band_sales_matches']}, customers match "
                f"{a['age_band_customers_match']}."
            )
            continue
        lines.append(
            f"- `{name}`: job `{a['job_id']}`, fingerprint "
            f"`{a['statement_fingerprint']}`, processed {a['bytes_processed']} "
            f"bytes (cache hit {a['cache_hit']}), window [{a['window_start']}, {a['window_end_exclusive']}), "
            f"{len(a['rows'])} rows, scope {a['product_scope']}."
        )
    total = sum(c["bytes_billed"] for c in report["query_costs"])
    lines += ["", f"Profile queries billed {total} bytes in total.", ""]
    return "\n".join(lines)
