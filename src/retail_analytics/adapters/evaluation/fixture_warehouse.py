# ruff: noqa: S608
"""An offline warehouse for evaluation: the four source tables in DuckDB.

``FixtureWarehouse`` implements the same ports as the BigQuery adapters
(``WarehouseQueryJobs``, ``SourceMetadataProvider`` and
``ProductBrandSource``), so an agent run
against it goes through the identical application path: discovery, the
restricted compiler with trusted product binding and keyed references,
durable job records, the result privacy boundary and evidence. Compiled
statements (BigQuery dialect) are transpiled to DuckDB and executed.

The tables live in an attached catalog named like the public dataset, so the
compiler's dataset name needs no change. Two loaders fill it:

- ``heldout_fixture_warehouse``: the synthetic held-out fixture
  (``evaluation/heldout/fixture``), including its canary names and e-mails;
- ``frozen_extract_warehouse``: the frozen, sanitized real-data extract
  (``evaluation/realdata/extract``). Columns the extract never contains
  (names, e-mails, addresses, brands, ...) exist as NULL so the source layout
  matches the live tables.

It is a result oracle for evaluation, not a model of BigQuery's engine. DuckDB
is a development dependency and is imported lazily.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from retail_analytics.application.access_check import PUBLIC_DATASET
from retail_analytics.application.contracts.brand_access import ProductBrandCatalog
from retail_analytics.application.contracts.result_privacy import QueryRows
from retail_analytics.application.contracts.warehouse_jobs import (
    JobRef,
    JobSnapshot,
    JobState,
    JobStatistics,
    JobSubmission,
)
from retail_analytics.application.discovery import SourceMetadataUnavailable
from retail_analytics.application.warehouse_jobs import JobAlreadyExists
from retail_analytics.domain.access import branded_products
from retail_analytics.domain.catalog import SourceColumn, SourceSchema, SourceType

PROJECT, DATASET = PUBLIC_DATASET.split(".", 1)
_ESTIMATED_BYTES: Final = 1024 * 1024

# Source layout (columns the catalog maps plus personal-data columns it never
# exposes, so a guard failure would be visible in evaluation).
_TABLES: Final[Mapping[str, tuple[tuple[str, str], ...]]] = {
    "products": (
        ("id", "BIGINT"),
        ("name", "VARCHAR"),
        ("category", "VARCHAR"),
        ("brand", "VARCHAR"),
        ("department", "VARCHAR"),
        ("retail_price", "DOUBLE"),
        ("cost", "DOUBLE"),
    ),
    "users": (
        ("id", "BIGINT"),
        ("first_name", "VARCHAR"),
        ("last_name", "VARCHAR"),
        ("email", "VARCHAR"),
        ("age", "BIGINT"),
        ("state", "VARCHAR"),
        ("country", "VARCHAR"),
        ("street_address", "VARCHAR"),
        ("city", "VARCHAR"),
        ("postal_code", "VARCHAR"),
    ),
    "orders": (
        ("order_id", "BIGINT"),
        ("user_id", "BIGINT"),
        ("status", "VARCHAR"),
        ("created_at", "TIMESTAMP"),
        ("num_of_item", "BIGINT"),
    ),
    "order_items": (
        ("id", "BIGINT"),
        ("order_id", "BIGINT"),
        ("user_id", "BIGINT"),
        ("product_id", "BIGINT"),
        ("status", "VARCHAR"),
        ("sale_price", "DOUBLE"),
        ("created_at", "TIMESTAMP"),
    ),
}
_TYPES: Final = {
    "BIGINT": SourceType.INT64,
    "VARCHAR": SourceType.STRING,
    "DOUBLE": SourceType.FLOAT64,
    "TIMESTAMP": SourceType.TIMESTAMP,
}


def _table(name: str) -> str:
    return f'"{PROJECT}".{DATASET}.{name}'


@dataclass
class _Job:
    submission: JobSubmission
    result: QueryRows | None
    error_reason: str | None


class FixtureWarehouse:
    """In-process warehouse over a DuckDB copy of the source tables."""

    def __init__(self, connection: Any, data_ref: str) -> None:
        self._db = connection
        self.data_ref = data_ref
        self._jobs: dict[str, _Job] = {}
        self.submitted = 0

    @classmethod
    def empty(cls, data_ref: str) -> FixtureWarehouse:
        import duckdb

        db = duckdb.connect(":memory:")
        db.execute(f"ATTACH ':memory:' AS \"{PROJECT}\"")
        db.execute(f'CREATE SCHEMA "{PROJECT}".{DATASET}')
        for table, columns in _TABLES.items():
            body = ", ".join(f"{name} {kind}" for name, kind in columns)
            db.execute(f"CREATE TABLE {_table(table)} ({body})")
        return cls(db, data_ref)

    def insert(self, table: str, rows: Sequence[Mapping[str, Any]]) -> None:
        columns = [name for name, _ in _TABLES[table]]
        holes = ", ".join("?" for _ in columns)
        self._db.executemany(
            f"INSERT INTO {_table(table)} VALUES ({holes})",
            [[row.get(c) for c in columns] for row in rows],
        )

    def product_names(self) -> list[tuple[str, str]]:
        """(product id, name) of every named product (trusted harness use)."""
        rows = self._db.execute(
            f"SELECT id, name FROM {_table('products')} WHERE name IS NOT NULL"
        ).fetchall()
        return [(str(i), str(n)) for i, n in rows]

    # ProductBrandSource

    async def read_product_brands(self) -> ProductBrandCatalog:
        """``products.brand`` of this data (the frozen extract has no brands,
        so it yields an empty catalog)."""
        rows = self._db.execute(
            f"SELECT CAST(id AS VARCHAR), brand FROM {_table('products')}"
        ).fetchall()
        brands, skipped = branded_products((str(i), b) for i, b in rows)
        return ProductBrandCatalog(
            brands=brands, source_ref=self.data_ref, products_without_brand=skipped
        )

    # SourceMetadataProvider

    async def read_schema(self, tables: frozenset[str]) -> SourceSchema:
        found: dict[str, tuple[SourceColumn, ...]] = {}
        try:
            for table in sorted(tables & set(_TABLES)):
                rows = self._db.execute(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_catalog = ? AND table_schema = ? AND table_name = ? "
                    "ORDER BY ordinal_position",
                    [PROJECT, DATASET, table],
                ).fetchall()
                found[table] = tuple(
                    SourceColumn(name, _TYPES.get(kind, SourceType.OTHER))
                    for name, kind in rows
                )
        except Exception as error:
            raise SourceMetadataUnavailable("fixture metadata unavailable") from error
        return SourceSchema(found)

    # WarehouseQueryJobs

    async def dry_run(self, submission: JobSubmission) -> int:
        return _ESTIMATED_BYTES

    async def submit(self, submission: JobSubmission) -> JobSnapshot:
        job_id = submission.ref.job_id
        if job_id in self._jobs:
            raise JobAlreadyExists("duplicate")
        self.submitted += 1
        try:
            result: QueryRows | None = self._execute(submission)
            reason = None
        except Exception:
            # A statement the oracle cannot run; never echo engine messages.
            result, reason = None, "invalidQuery"
        self._jobs[job_id] = _Job(submission, result, reason)
        return self._snapshot(job_id)

    async def lookup(self, ref: JobRef) -> JobSnapshot | None:
        if ref.job_id not in self._jobs:
            return None
        return self._snapshot(ref.job_id)

    async def fetch_rows(self, ref: JobRef, *, max_rows: int) -> QueryRows:
        job = self._jobs[ref.job_id]
        if job.result is None:
            raise LookupError("job has no result")
        rows = list(job.result.rows)
        return QueryRows(job.result.columns, rows[:max_rows], len(rows) <= max_rows)

    async def cancel(self, ref: JobRef) -> None:
        return None

    def _execute(self, submission: JobSubmission) -> QueryRows:
        import sqlglot

        tree = sqlglot.parse_one(submission.sql, read="bigquery")
        sql = tree.sql(dialect="duckdb")
        params = {
            p.name: list(p.value) if isinstance(p.value, tuple) else p.value
            for p in submission.parameters
            if f"${p.name}" in sql
        }
        cursor = self._db.execute(sql, params)
        return QueryRows(
            tuple(d[0] for d in cursor.description), cursor.fetchall(), True
        )

    def _snapshot(self, job_id: str) -> JobSnapshot:
        job = self._jobs[job_id]
        return JobSnapshot(
            ref=job.submission.ref,
            state=JobState.DONE,
            fingerprint=job.submission.fingerprint,
            error_reason=job.error_reason,
            statistics=JobStatistics(
                bytes_processed=_ESTIMATED_BYTES,
                bytes_billed=_ESTIMATED_BYTES,
                cache_hit=False,
            ),
        )


def heldout_fixture_warehouse(fixture_dir: Path) -> FixtureWarehouse:
    """The synthetic held-out fixture (``heldout-fixture-1``)."""

    def rows(name: str) -> list[dict[str, Any]]:
        data = json.loads((fixture_dir / name).read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError(f"{name}: expected a list")
        return data

    warehouse = FixtureWarehouse.empty("heldout-fixture-1")
    warehouse.insert(
        "products",
        [{**p, "id": p["product_id"], "cost": None} for p in rows("products.json")],
    )
    warehouse.insert(
        "users", [{**c, "id": c["customer_id"]} for c in rows("customers.json")]
    )
    orders: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    for order in rows("orders.json"):
        created = order["created_at"].replace("T", " ")
        orders.append(
            {
                "order_id": order["order_id"],
                "user_id": order["customer_id"],
                "created_at": created,
                "num_of_item": len(order["items"]),
            }
        )
        for item in order["items"]:
            items.append(
                {
                    "id": len(items) + 1,
                    "order_id": order["order_id"],
                    "user_id": order["customer_id"],
                    "product_id": item["product_id"],
                    "status": item["status"],
                    "sale_price": float(item["sale_price"]),
                    "created_at": created,
                }
            )
    warehouse.insert("orders", orders)
    warehouse.insert("order_items", items)
    return warehouse


def frozen_extract_warehouse(extract_dir: Path, data_ref: str) -> FixtureWarehouse:
    """The frozen real-data extract as the four source tables.

    Loads the gzip CSV files with DuckDB's reader; absent source columns stay
    NULL. ``data_ref`` is the extract's data version (manifest ``fixture_ref``).
    """
    warehouse = FixtureWarehouse.empty(data_ref)
    db = warehouse._db
    for table, columns in _TABLES.items():
        path = extract_dir / f"{table}.csv.gz"
        present = db.execute(
            "SELECT * FROM read_csv(?, header = true, compression = 'gzip') LIMIT 0",
            [str(path)],
        ).description
        names = {d[0] for d in present}
        select = ", ".join(
            f"CAST({name} AS {kind})" if name in names else f"CAST(NULL AS {kind})"
            for name, kind in columns
        )
        db.execute(
            f"INSERT INTO {_table(table)} SELECT {select} FROM "
            "read_csv(?, header = true, compression = 'gzip')",
            [str(path)],
        )
    return warehouse


__all__ = [
    "FixtureWarehouse",
    "frozen_extract_warehouse",
    "heldout_fixture_warehouse",
]
