# ruff: noqa: E501
"""Build the sanitized frozen extract from BigQuery, and re-run reference SQL live.

Sanitization happens inside BigQuery, before any row reaches this process:

- order, customer and item identifiers are replaced by dense pseudonyms
  (``DENSE_RANK`` over the window's population; the original values are never
  selected), so a row cannot be traced back to a source record;
- ``users.age`` becomes the start of its 5-year band (top-coded at 90);
- names, e-mails, addresses, coordinates and traffic source are never selected;
- timestamps are cut to whole seconds (windows begin on whole seconds, so no
  order changes side of a window boundary);
- ``sale_price`` is rounded to 2 decimals.

Every statement is read-only and carries ``maximum_bytes_billed``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final

from google.cloud import bigquery

from retail_analytics.application.evaluation.realdata import (
    EXTRACT_COLUMNS,
    BenchmarkSpec,
    EngineResult,
    ExtractQueryRecord,
    SourceTableInfo,
    sql_fingerprint,
)

BYTES_CAP: Final = 512 * 1024 * 1024
TABLES: Final = ("orders", "order_items", "users", "products")

SANITIZATION: Final = (
    "order, customer and item identifiers replaced by dense pseudonyms inside BigQuery",
    "users.age reduced to the start of a 5-year band (top-coded at 90)",
    "no name, e-mail, address, coordinate or traffic-source column selected",
    "order timestamps truncated to whole seconds; sale_price rounded to 2 decimals",
    "only orders created inside the window, with all of their items, are copied",
)

_PREFIX = """WITH win_orders AS (
  SELECT order_id, user_id, created_at FROM {ds}.orders
  WHERE created_at >= TIMESTAMP '{ws} 00:00:00' AND created_at < TIMESTAMP '{we} 00:00:00'),
order_keys AS (SELECT order_id, DENSE_RANK() OVER (ORDER BY order_id) AS order_no FROM win_orders),
user_keys AS (
  SELECT user_id, DENSE_RANK() OVER (ORDER BY user_id) AS user_no
  FROM (SELECT DISTINCT user_id FROM win_orders))
"""
_BODIES: Final[Mapping[str, str]] = {
    "orders": """SELECT ok.order_no AS order_id, uk.user_no AS user_id,
  FORMAT_TIMESTAMP('%Y-%m-%d %H:%M:%S', w.created_at) AS created_at
FROM win_orders AS w JOIN order_keys AS ok ON ok.order_id = w.order_id
JOIN user_keys AS uk ON uk.user_id = w.user_id
ORDER BY order_id""",
    "order_items": """SELECT ROW_NUMBER() OVER (ORDER BY oi.id) AS id, ok.order_no AS order_id,
  uk.user_no AS user_id, oi.product_id AS product_id, oi.status AS status,
  ROUND(oi.sale_price, 2) AS sale_price
FROM {ds}.order_items AS oi JOIN win_orders AS w ON w.order_id = oi.order_id
JOIN order_keys AS ok ON ok.order_id = w.order_id
JOIN user_keys AS uk ON uk.user_id = w.user_id
ORDER BY id""",
    "users": """SELECT uk.user_no AS id,
  LEAST(CAST(FLOOR(u.age / 5.0) AS INT64) * 5, 90) AS age, u.state AS state
FROM user_keys AS uk JOIN {ds}.users AS u ON u.id = uk.user_id
ORDER BY id""",
    "products": """SELECT p.id AS id, p.name AS name, p.category AS category
FROM {ds}.products AS p
WHERE p.id IN (SELECT oi.product_id FROM {ds}.order_items AS oi JOIN win_orders AS w ON w.order_id = oi.order_id)
ORDER BY id""",
}


def extraction_sql(spec: BenchmarkSpec, table: str) -> str:
    ds = "`" + spec.extract.source_dataset + "`"
    text = _PREFIX + _BODIES[table]
    return (
        text.replace("{ds}", ds)
        .replace("{ws}", spec.extract.window_start)
        .replace("{we}", spec.extract.window_end_exclusive)
    )


class BigQueryExtractor:
    def __init__(self, client: bigquery.Client, project: str, location: str) -> None:
        self._client = client
        self._project = project
        self._location = location

    def _run(self, sql: str, task: str) -> tuple[list[Any], Any]:
        job = self._client.query(
            sql,
            job_config=bigquery.QueryJobConfig(
                maximum_bytes_billed=BYTES_CAP,
                labels={"app": "retail-analytics", "task": task},
            ),
            project=self._project,
            location=self._location,
        )
        return list(job.result()), job

    def extract_table(
        self, spec: BenchmarkSpec, table: str
    ) -> tuple[list[tuple[Any, ...]], ExtractQueryRecord]:
        sql = extraction_sql(spec, table)
        rows, job = self._run(sql, "t35-extract")
        columns = EXTRACT_COLUMNS[table]
        data = [tuple(row[c] for c in columns) for row in rows]
        record = ExtractQueryRecord(
            table=table,
            fingerprint=sql_fingerprint(sql),
            job_id=str(job.job_id),
            bytes_processed=int(job.total_bytes_processed or 0),
            bytes_billed=int(job.total_bytes_billed or 0),
        )
        return data, record

    def source_tables(self, spec: BenchmarkSpec) -> dict[str, SourceTableInfo]:
        out: dict[str, SourceTableInfo] = {}
        for table in TABLES:
            info = self._client.get_table(f"{spec.extract.source_dataset}.{table}")
            modified = info.modified
            out[table] = SourceTableInfo(
                num_rows=info.num_rows,
                modified=modified.astimezone(UTC).isoformat() if modified else None,
            )
        return out

    @staticmethod
    def now() -> str:
        return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class BigQueryEngine:
    """Live execution of reference SQL for the drift report."""

    def __init__(self, client: bigquery.Client, project: str, location: str) -> None:
        self._extractor = BigQueryExtractor(client, project, location)

    @staticmethod
    def dataset_ref(spec: BenchmarkSpec) -> str:
        return "`" + spec.extract.source_dataset + "`"

    def run_one(self, sql: str) -> EngineResult:
        rows, job = self._extractor._run(sql, "t35-drift")
        if len(rows) != 1:
            raise ValueError(f"expected one row, got {len(rows)}")
        return EngineResult(
            row=dict(rows[0].items()),
            bytes_processed=int(job.total_bytes_processed or 0),
            bytes_billed=int(job.total_bytes_billed or 0),
            job_id=str(job.job_id),
        )


def sequence_columns(table: str) -> Sequence[str]:
    return EXTRACT_COLUMNS[table]
