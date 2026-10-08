"""Composition of run budget enforcement from settings and persistence."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from retail_analytics.application.budgets import (
    RetrySettings,
    RunBudgets,
    RunBudgetStore,
)
from retail_analytics.bootstrap.config import BackendSettings
from retail_analytics.domain.budgets import RunLimits


def run_limits(settings: BackendSettings) -> RunLimits:
    """Limits new runs are opened with (existing runs keep theirs)."""
    return RunLimits(
        active_seconds=settings.run_active_seconds,
        provider_requests=settings.run_max_provider_requests,
        tokens=settings.run_max_tokens,
        queries=settings.run_max_queries,
        bytes_per_query=settings.query_max_bytes,
        bytes_per_run=settings.run_max_bytes,
        corrections_per_query=settings.query_max_corrections,
        transient_attempts=settings.max_transient_attempts,
        query_deadline_seconds=settings.query_deadline_seconds,
        result_rows=settings.result_max_rows,
        result_bytes=settings.result_max_bytes,
    )


def build_run_budgets(
    settings: BackendSettings,
    store: RunBudgetStore,
    *,
    clock: Callable[[], datetime] | None = None,
) -> RunBudgets:
    """``store`` is ``Persistence.budgets`` in the API and worker."""
    retry = RetrySettings(settings.retry_base_seconds, settings.retry_max_seconds)
    if clock is None:
        return RunBudgets(store, run_limits(settings), retry=retry)
    return RunBudgets(store, run_limits(settings), retry=retry, clock=clock)
