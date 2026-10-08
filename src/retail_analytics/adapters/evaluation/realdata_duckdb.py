# ruff: noqa: S608
"""Run reference SQL over the frozen extract in an in-memory DuckDB database.

DuckDB is a development dependency: it is imported lazily so that importing the
package never needs it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

from retail_analytics.adapters.evaluation.realdata_files import (
    EXTRACT_DIR,
    table_columns,
)
from retail_analytics.application.contracts.evaluation import EngineResult
from retail_analytics.application.evaluation.realdata import EXTRACT_COLUMNS

SCHEMA: Final = "thelook"
_TYPES: Final = {
    "orders": "order_id BIGINT, user_id BIGINT, created_at TIMESTAMP",
    "order_items": (
        "id BIGINT, order_id BIGINT, user_id BIGINT, product_id BIGINT, "
        "status VARCHAR, sale_price DECIMAL(12,2)"
    ),
    "users": "id BIGINT, age BIGINT, state VARCHAR",
    "products": "id BIGINT, name VARCHAR, category VARCHAR",
}


class DuckDbExtractEngine:
    """The extract as schema ``thelook`` (same table and column names as the source)."""

    def __init__(self, root: Path) -> None:
        import duckdb

        self._db: Any = duckdb.connect(":memory:")
        self._db.execute(f"CREATE SCHEMA {SCHEMA}")
        for table, columns in EXTRACT_COLUMNS.items():
            path = root / EXTRACT_DIR / f"{table}.csv.gz"
            if tuple(table_columns(path)) != columns:
                raise ValueError(f"{path.name}: unexpected columns")
            self._db.execute(f"CREATE TABLE {SCHEMA}.{table}({_TYPES[table]})")
            self._db.execute(
                f"COPY {SCHEMA}.{table} FROM '{path}' "
                "(FORMAT CSV, HEADER true, COMPRESSION gzip)"
            )

    @property
    def dataset_ref(self) -> str:
        return SCHEMA

    def row_counts(self) -> dict[str, int]:
        return {
            table: int(
                self._db.execute(f"SELECT COUNT(*) FROM {SCHEMA}.{table}").fetchone()[0]
            )
            for table in EXTRACT_COLUMNS
        }

    def run_one(self, sql: str) -> EngineResult:
        cursor = self._db.execute(sql)
        names = [d[0] for d in cursor.description]
        rows = cursor.fetchall()
        if len(rows) != 1:
            raise ValueError(f"expected one row, got {len(rows)}")
        return EngineResult(row=dict(zip(names, rows[0], strict=True)))
