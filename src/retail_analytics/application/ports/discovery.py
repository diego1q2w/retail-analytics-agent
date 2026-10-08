from __future__ import annotations

from typing import Protocol

from retail_analytics.domain.catalog import SourceSchema


class SourceMetadataProvider(Protocol):
    """Reads column names and types of the given source tables (metadata only)."""

    async def read_schema(self, tables: frozenset[str]) -> SourceSchema:
        """Raise SourceMetadataUnavailable if metadata cannot be read."""
        ...
