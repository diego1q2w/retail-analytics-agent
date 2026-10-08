"""Credential and access verification for the external services.

Narrow ports for the two things a developer must have working before live runs:
read access to the public BigQuery tables (plus a cost-bounded query check) and a
model provider that answers a minimal request. Adapters translate every SDK
failure into :class:`AccessError`, whose message and remedy never contain secrets.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

PUBLIC_DATASET = "bigquery-public-data.thelook_ecommerce"
REQUIRED_TABLES: tuple[str, ...] = ("orders", "order_items", "products", "users")
DRY_RUN_BYTES_LIMIT = 100 * 1024 * 1024


class AccessError(Exception):
    """An access problem with a human-readable cause and a fix. No secrets."""

    def __init__(self, problem: str, remedy: str) -> None:
        self.problem = problem
        self.remedy = remedy
        super().__init__(problem)


@dataclass(frozen=True)
class TableMetadata:
    table: str
    rows: int
    columns: int


class WarehouseAccess(Protocol):
    def credentials_ready(self) -> None:
        """Raise AccessError if application default credentials are unusable."""

    def table_metadata(self, table: str) -> TableMetadata: ...

    def dry_run_bytes(self, sql: str) -> int:
        """Bytes a query would scan, without running it. Raises AccessError."""


class ModelAccess(Protocol):
    @property
    def model_name(self) -> str: ...

    def ping(self) -> None:
        """Send a minimal non-sensitive request. Raises AccessError on failure."""


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str
    remedy: str = ""


def _guarded(name: str, action: Callable[[], str]) -> CheckResult:
    try:
        return CheckResult(name, True, action())
    except AccessError as exc:
        return CheckResult(name, False, exc.problem, exc.remedy)


def check_warehouse(
    access: WarehouseAccess,
    tables: Sequence[str] = REQUIRED_TABLES,
    dataset: str = PUBLIC_DATASET,
) -> list[CheckResult]:
    """ADC, metadata for every table, and a bounded dry run, in that order.

    Table and dry-run checks are skipped when credentials already failed, since
    they would only repeat the same error.
    """

    def credentials() -> str:
        access.credentials_ready()
        return "application default credentials found"

    first = _guarded("bigquery credentials", credentials)
    if not first.ok:
        return [first]
    results = [first]
    for table in tables:

        def metadata(table: str = table) -> str:
            info = access.table_metadata(f"{dataset}.{table}")
            return f"{info.rows} rows, {info.columns} columns"

        results.append(_guarded(f"bigquery table {table}", metadata))

    def dry_run() -> str:
        sql = f"SELECT COUNT(*) FROM `{dataset}.orders`"  # noqa: S608 - fixed names
        scanned = access.dry_run_bytes(sql)
        if scanned > DRY_RUN_BYTES_LIMIT:
            raise AccessError(
                f"dry run would scan {scanned} bytes, above the "
                f"{DRY_RUN_BYTES_LIMIT} byte limit",
                "check that the query targets the expected table",
            )
        return f"dry run ok, {scanned} bytes (limit {DRY_RUN_BYTES_LIMIT})"

    results.append(_guarded("bigquery dry run", dry_run))
    return results


def check_model(access: ModelAccess) -> CheckResult:
    def ping() -> str:
        access.ping()
        return f"model {access.model_name} answered"

    return _guarded("gemini request", ping)
