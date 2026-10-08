"""BigQuery metadata adapter against a fake client (no network)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from google.api_core import exceptions as api_exceptions

from retail_analytics.adapters.bigquery.metadata import (
    BigQuerySourceMetadata,
    normalize_type,
)
from retail_analytics.application.discovery import SourceMetadataUnavailable
from retail_analytics.domain.catalog import SourceType


class FakeClient:
    def __init__(self, tables: dict[str, list[tuple[str, str, str]]]) -> None:
        self.tables = tables
        self.asked: list[str] = []

    def get_table(self, ref: str) -> SimpleNamespace:
        self.asked.append(ref)
        name = ref.rsplit(".", 1)[1]
        if name == "boom":
            raise RuntimeError("secret detail")
        if name not in self.tables:
            raise api_exceptions.NotFound("nope")  # type: ignore[no-untyped-call]
        return SimpleNamespace(
            schema=[
                SimpleNamespace(name=n, field_type=t, mode=m)
                for n, t, m in self.tables[name]
            ]
        )


def test_type_normalization() -> None:
    assert normalize_type("INTEGER", "NULLABLE") is SourceType.INT64
    assert normalize_type("FLOAT", None) is SourceType.FLOAT64
    assert normalize_type("TIMESTAMP", "NULLABLE") is SourceType.TIMESTAMP
    assert normalize_type("STRING", "REPEATED") is SourceType.OTHER
    assert normalize_type("GEOGRAPHY", "NULLABLE") is SourceType.OTHER


@pytest.mark.asyncio
async def test_reads_columns_and_treats_missing_table_as_absent() -> None:
    client = FakeClient({"orders": [("order_id", "INTEGER", "NULLABLE")]})
    adapter = BigQuerySourceMetadata("p", "US", "proj.data", client=client)  # type: ignore[arg-type]
    schema = await adapter.read_schema(frozenset({"orders", "users"}))
    assert set(schema.tables) == {"orders"}
    assert schema.tables["orders"][0].type is SourceType.INT64
    assert "proj.data.orders" in client.asked


@pytest.mark.asyncio
async def test_other_failures_become_unavailable_without_detail() -> None:
    adapter = BigQuerySourceMetadata("p", "US", "d", client=FakeClient({}))  # type: ignore[arg-type]
    with pytest.raises(SourceMetadataUnavailable) as raised:
        await adapter.read_schema(frozenset({"boom"}))
    assert "secret" not in str(raised.value)
