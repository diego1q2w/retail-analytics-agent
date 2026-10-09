"""Composition of query compilation, execution and the result privacy boundary."""

from __future__ import annotations

from retail_analytics.adapters.bigquery.jobs import BigQueryQueryJobs
from retail_analytics.adapters.sql_compiler import (
    CompilerLimits,
    ReferenceKeyring,
    ScopedSqlglotCompilers,
)
from retail_analytics.application.access_check import PUBLIC_DATASET
from retail_analytics.application.authorization import AccessResolver
from retail_analytics.application.budgets import RunBudgets
from retail_analytics.application.discovery import DiscoveryService
from retail_analytics.application.ports.query_execution import QueryAdmission
from retail_analytics.application.ports.warehouse_jobs import WarehouseQueryJobs
from retail_analytics.application.query_execution import (
    FreshQueryAuthority,
    QueryExecutionService,
    QueryExecutionSettings,
)
from retail_analytics.application.result_privacy import (
    ResultLimits,
    ResultPrivacyBoundary,
)
from retail_analytics.application.scope_values import ScopeValueCheck
from retail_analytics.bootstrap.config import BackendSettings, ConfigError
from retail_analytics.bootstrap.persistence import Persistence


def build_query_compilers(
    settings: BackendSettings, *, dataset: str = PUBLIC_DATASET
) -> ScopedSqlglotCompilers:
    """Per-executive compilers; references need REFERENCE_KEY."""
    key = settings.reference_key
    keyring = None if key is None else ReferenceKeyring(key.get_secret_value().encode())
    limits = CompilerLimits(maximum_bytes_billed=settings.query_max_bytes)
    return ScopedSqlglotCompilers(dataset, keyring, limits=limits)


def build_result_boundary(
    settings: BackendSettings | None = None,
) -> ResultPrivacyBoundary:
    """Result caps from settings (500 rows / 256 KiB by default)."""
    if settings is None:
        return ResultPrivacyBoundary()
    return ResultPrivacyBoundary(
        ResultLimits(
            max_rows=settings.result_max_rows, max_bytes=settings.result_max_bytes
        )
    )


def build_query_execution(
    settings: BackendSettings,
    persistence: Persistence,
    resolver: AccessResolver,
    discovery: DiscoveryService,
    *,
    warehouse: WarehouseQueryJobs | None = None,
    budgets: RunBudgets | None = None,
    admission: QueryAdmission | None = None,
) -> QueryExecutionService:
    """Durable query execution against the configured BigQuery project.

    ``budgets`` (``bootstrap.budgets.build_run_budgets``) charges every job
    submission to the run and settles its actual bytes; composition roots
    must pass it. ``admission`` overrides only the pre-submission check;
    ``warehouse`` replaces the BigQuery adapter (tests, other warehouses).
    """
    project = settings.bigquery_project
    if project is None:
        raise ConfigError(["GOOGLE_CLOUD_PROJECT: required"])
    location = settings.bigquery_location
    return QueryExecutionService(
        settings=QueryExecutionSettings(
            project=project,
            location=location,
            max_rows=settings.result_max_rows,
            query_deadline_seconds=settings.query_deadline_seconds,
            max_transient_attempts=settings.max_transient_attempts,
        ),
        authority=FreshQueryAuthority(resolver, discovery),
        compilers=build_query_compilers(settings),
        boundary=build_result_boundary(settings),
        warehouse=warehouse or BigQueryQueryJobs(project, location),
        operations=persistence.tool_executions,
        jobs=persistence.query_jobs,
        admission=admission or budgets,
        usage=budgets,
        scope_values=ScopeValueCheck(persistence.brand_access),
    )
