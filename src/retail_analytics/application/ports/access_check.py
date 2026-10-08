from __future__ import annotations

from typing import Protocol

from retail_analytics.application.contracts.access_check import TableMetadata


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
