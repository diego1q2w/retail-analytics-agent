"""BigQuery implementation of ``SourceMetadataProvider``.

Reads table metadata only (``tables.get``): free, and never touches row data.
SDK exceptions become ``SourceMetadataUnavailable`` with no provider detail,
except a missing table, which is reported as an empty table list so drift
evaluation disables the dependent fields rather than failing everything.
"""

from __future__ import annotations

import asyncio

from google.api_core import exceptions as api_exceptions
from google.cloud import bigquery

from retail_analytics.adapters.google_access import create_bigquery_client
from retail_analytics.application.discovery import SourceMetadataUnavailable
from retail_analytics.domain.catalog import SourceColumn, SourceSchema, SourceType

_TYPES: dict[str, SourceType] = {
    "STRING": SourceType.STRING,
    "INTEGER": SourceType.INT64,
    "INT64": SourceType.INT64,
    "FLOAT": SourceType.FLOAT64,
    "FLOAT64": SourceType.FLOAT64,
    "NUMERIC": SourceType.NUMERIC,
    "BIGNUMERIC": SourceType.NUMERIC,
    "BOOLEAN": SourceType.BOOL,
    "BOOL": SourceType.BOOL,
    "DATE": SourceType.DATE,
    "TIMESTAMP": SourceType.TIMESTAMP,
}


def normalize_type(field_type: str, mode: str | None) -> SourceType:
    """Repeated columns are never compatible with scalar logical fields."""
    if mode == "REPEATED":
        return SourceType.OTHER
    return _TYPES.get(field_type.upper(), SourceType.OTHER)


class BigQuerySourceMetadata:
    def __init__(
        self,
        project: str,
        location: str,
        dataset: str,
        client: bigquery.Client | None = None,
    ) -> None:
        self._project = project
        self._location = location
        self._dataset = dataset
        self._client = client

    def _get_client(self) -> bigquery.Client:
        if self._client is None:
            self._client = create_bigquery_client(self._project, self._location)
        return self._client

    async def read_schema(self, tables: frozenset[str]) -> SourceSchema:
        try:
            read = await asyncio.gather(
                *(asyncio.to_thread(self._read_table, t) for t in sorted(tables))
            )
        except SourceMetadataUnavailable:
            raise
        except Exception:
            raise SourceMetadataUnavailable("source metadata read failed") from None
        return SourceSchema({name: cols for name, cols in read if cols is not None})

    def _read_table(self, table: str) -> tuple[str, tuple[SourceColumn, ...] | None]:
        try:
            info = self._get_client().get_table(f"{self._dataset}.{table}")
        except api_exceptions.NotFound:
            return table, None
        columns = tuple(
            SourceColumn(f.name, normalize_type(f.field_type or "", f.mode))
            for f in info.schema
        )
        return table, columns
