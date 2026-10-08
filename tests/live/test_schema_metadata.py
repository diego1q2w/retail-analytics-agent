"""Live metadata-only check of the catalog against the real dataset.

Reads table schemas (free; never row data). Skipped without a configured
project and application default credentials. The project comes from the
environment or the ignored ``.env`` through the typed loader.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from retail_analytics.adapters.bigquery.metadata import BigQuerySourceMetadata
from retail_analytics.application.access_check import PUBLIC_DATASET
from retail_analytics.application.discovery import SourceMetadataUnavailable
from retail_analytics.bootstrap.config import load_backend_settings
from retail_analytics.domain.catalog import evaluate_health
from retail_analytics.domain.logical_catalog import default_logical_catalog

pytestmark = [pytest.mark.live, pytest.mark.asyncio]
ROOT = Path(__file__).resolve().parents[2]


async def test_catalog_is_compatible_with_live_metadata() -> None:
    settings = load_backend_settings(env_file=ROOT / ".env")
    if settings.bigquery_project is None:
        pytest.skip("RETAIL_ANALYTICS_BIGQUERY_PROJECT not set")
    catalog = default_logical_catalog()
    adapter = BigQuerySourceMetadata(
        settings.bigquery_project, settings.bigquery_location, PUBLIC_DATASET
    )
    try:
        schema = await adapter.read_schema(catalog.source_tables)
    except SourceMetadataUnavailable:
        pytest.skip("BigQuery metadata not reachable with current credentials")
    health = evaluate_health(catalog, schema)
    problems = [(i.kind.value, i.relation, i.field) for i in health.issues]
    assert not problems, problems
