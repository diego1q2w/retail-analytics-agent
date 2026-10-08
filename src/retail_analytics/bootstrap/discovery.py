"""Composition of schema discovery: catalog, metadata cache and capabilities."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from retail_analytics.adapters.bigquery.metadata import BigQuerySourceMetadata
from retail_analytics.application.access_check import PUBLIC_DATASET
from retail_analytics.application.discovery import (
    DEFAULT_MAX_STALE,
    DiscoveryService,
    SourceMetadataProvider,
    SourceSchemaCache,
)
from retail_analytics.bootstrap.config import BackendSettings, ConfigError
from retail_analytics.domain.logical_catalog import default_logical_catalog


def _utc_now() -> datetime:
    return datetime.now(UTC)


def build_discovery(
    settings: BackendSettings,
    *,
    provider: SourceMetadataProvider | None = None,
    clock: Callable[[], datetime] = _utc_now,
) -> DiscoveryService:
    """Wire discovery; pass ``provider`` to use something other than BigQuery."""
    if provider is None:
        if settings.bigquery_project is None:
            raise ConfigError(["RETAIL_ANALYTICS_BIGQUERY_PROJECT: required"])
        provider = BigQuerySourceMetadata(
            settings.bigquery_project, settings.bigquery_location, PUBLIC_DATASET
        )
    interval = timedelta(seconds=settings.schema_refresh_seconds)
    catalog = default_logical_catalog()
    cache = SourceSchemaCache(
        catalog,
        provider,
        clock,
        refresh_interval=interval,
        max_stale=max(DEFAULT_MAX_STALE, interval),
    )
    return DiscoveryService(catalog, cache, clock, stale_after=interval)
