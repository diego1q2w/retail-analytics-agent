"""Composition of run budget enforcement from settings and persistence."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from retail_analytics.adapters.models.pricing import GenaiPricesCatalog, PriceOverride
from retail_analytics.application.budgets import (
    RetrySettings,
    RunBudgets,
    usd_to_micros,
)
from retail_analytics.application.ports.budgets import RunBudgetStore
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
        model_cost_micros=usd_to_micros(settings.run_max_model_cost_usd),
    )


def model_pricing(settings: BackendSettings) -> GenaiPricesCatalog:
    """Maintained price list plus the configured overrides."""
    return GenaiPricesCatalog(
        {
            model: PriceOverride(
                input_per_mtok=price.input,
                output_per_mtok=price.output,
                cached_input_per_mtok=price.cached_input,
            )
            for model, price in settings.model_price_overrides.items()
        }
    )


def build_run_budgets(
    settings: BackendSettings,
    store: RunBudgetStore,
    *,
    clock: Callable[[], datetime] | None = None,
) -> RunBudgets:
    """``store`` is ``Persistence.budgets`` in the API and worker."""
    retry = RetrySettings(settings.retry_base_seconds, settings.retry_max_seconds)
    pricing = model_pricing(settings)
    if clock is None:
        return RunBudgets(store, run_limits(settings), retry=retry, pricing=pricing)
    return RunBudgets(
        store, run_limits(settings), retry=retry, clock=clock, pricing=pricing
    )
