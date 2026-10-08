"""Composition of query compilation, execution and the result privacy boundary."""

from __future__ import annotations

from retail_analytics.adapters.bigquery.jobs import BigQueryQueryJobs
from retail_analytics.adapters.sql_compiler import (
    ReferenceKeyring,
    ScopedSqlglotCompilers,
)
from retail_analytics.application.access_check import PUBLIC_DATASET
from retail_analytics.application.authorization import AccessResolver
from retail_analytics.application.discovery import DiscoveryService
from retail_analytics.application.query_execution import (
    FreshQueryAuthority,
    QueryAdmission,
    QueryExecutionService,
    QueryExecutionSettings,
)
from retail_analytics.application.result_privacy import ResultPrivacyBoundary
from retail_analytics.application.warehouse_jobs import WarehouseQueryJobs
from retail_analytics.bootstrap.config import BackendSettings, ConfigError
from retail_analytics.bootstrap.persistence import Persistence


def build_query_compilers(
    settings: BackendSettings, *, dataset: str = PUBLIC_DATASET
) -> ScopedSqlglotCompilers:
    """Per-executive compilers; references need RETAIL_ANALYTICS_REFERENCE_KEY."""
    key = settings.reference_key
    keyring = None if key is None else ReferenceKeyring(key.get_secret_value().encode())
    return ScopedSqlglotCompilers(dataset, keyring)


def build_result_boundary() -> ResultPrivacyBoundary:
    return ResultPrivacyBoundary()


def build_query_execution(
    settings: BackendSettings,
    persistence: Persistence,
    resolver: AccessResolver,
    discovery: DiscoveryService,
    *,
    warehouse: WarehouseQueryJobs | None = None,
    admission: QueryAdmission | None = None,
) -> QueryExecutionService:
    """Durable query execution against the configured BigQuery project.

    ``admission`` is where run budgets plug in; ``warehouse`` replaces the
    BigQuery adapter (tests, other warehouses).
    """
    project = settings.bigquery_project
    if project is None:
        raise ConfigError(["RETAIL_ANALYTICS_BIGQUERY_PROJECT: required"])
    location = settings.bigquery_location
    return QueryExecutionService(
        settings=QueryExecutionSettings(project=project, location=location),
        authority=FreshQueryAuthority(resolver, discovery),
        compilers=build_query_compilers(settings),
        boundary=build_result_boundary(),
        warehouse=warehouse or BigQueryQueryJobs(project, location),
        operations=persistence.tool_executions,
        jobs=persistence.query_jobs,
        admission=admission,
    )
