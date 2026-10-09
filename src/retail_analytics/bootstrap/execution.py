"""Execution-backend selection shared by the API, the worker and dev tooling.

``EXECUTION_BACKEND`` picks where investigations execute,
independently of fixture/live ``mode``:

- ``local`` (default): the API process hosts the local manager
  (``bootstrap.local_investigations``). PostgreSQL only; no Temporal server,
  namespace or worker, and nothing on this path imports Temporal.
- ``temporal`` (opt-in): the API schedules durable workflows that
  ``retail-analytics-worker`` executes (``bootstrap.temporal``).

Both run the same investigation services and agent, built here from the
settings (``investigation_model`` / ``investigation_wiring``). Startup refuses
to take over unfinished work of the other backend (``ensure_owned_work``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from pydantic_ai.models import Model

from retail_analytics.adapters.local.investigations import LocalInvestigationManager
from retail_analytics.adapters.models.fixture import fixture_model
from retail_analytics.adapters.postgres.database import Database
from retail_analytics.adapters.postgres.local_execution import (
    ManagerLockHeld,
    PostgresActiveExecutions,
)
from retail_analytics.application.budgets import RunBudgets
from retail_analytics.application.discovery import DiscoveryService
from retail_analytics.application.execution_backends import (
    ExecutionOwnership,
    IncompatibleActiveExecutions,
)
from retail_analytics.application.query_execution import QueryExecutionService
from retail_analytics.application.retrieval import GoldenRetriever
from retail_analytics.application.tools import CapabilityRegistry
from retail_analytics.bootstrap.access import (
    AccessServices,
    build_access,
    local_token_authority,
)
from retail_analytics.bootstrap.artifacts import ArtifactServices, build_artifacts
from retail_analytics.bootstrap.budgets import build_run_budgets
from retail_analytics.bootstrap.config import (
    BackendSettings,
    ConfigError,
    RuntimeMode,
    backend_env_name,
    missing_api_settings,
)
from retail_analytics.bootstrap.discovery import build_discovery
from retail_analytics.bootstrap.knowledge import build_knowledge
from retail_analytics.bootstrap.local_investigations import (
    build_local_investigations,
)
from retail_analytics.bootstrap.models import provider_chain
from retail_analytics.bootstrap.persistence import Persistence
from retail_analytics.bootstrap.query import build_query_execution
from retail_analytics.bootstrap.retrieval import build_retrieval
from retail_analytics.domain.runs import ExecutionBackend

BACKEND_VARIABLE = backend_env_name("execution_backend")
InvestigationModel = Model | Callable[[RunBudgets], Model]

LOCK_HELD_MESSAGE = (
    "another API process with local execution is already running against this "
    "database (one process hosts local investigations). Stop it first, or use "
    "a separate database."
)
WORKER_NOT_USED_MESSAGE = (
    "retail-analytics-worker is only used with "
    f"{BACKEND_VARIABLE}=temporal. With local execution (the default) "
    "investigations run inside retail-analytics-api: start it with "
    "./scripts/dev.sh (or retail-analytics-api). To use Temporal, set "
    f"{BACKEND_VARIABLE}=temporal (see README, 'Temporal execution')."
)


class ExecutionStartupError(Exception):
    """The selected backend cannot start. Actionable; contains no secrets."""


# --- settings -----------------------------------------------------------


def require_api_settings(settings: BackendSettings) -> None:
    missing = missing_api_settings(settings)
    backend = settings.execution_backend.value
    if missing:
        raise ConfigError(
            [
                f"{name}: required by the HTTP API ({backend} execution)"
                for name in missing
            ]
        )


# --- investigation services --------------------------------------------


def investigation_model(settings: BackendSettings) -> InvestigationModel:
    """The offline fixture model, or the live Gemini/GPT chain (validates its
    settings, so call it before connecting to anything)."""
    if settings.mode is RuntimeMode.LIVE:
        return provider_chain(settings)
    return fixture_model()


@dataclass(frozen=True)
class InvestigationWiring:
    """Services every backend hands to ``build_investigations``."""

    artifacts: ArtifactServices
    retriever: GoldenRetriever
    discovery: DiscoveryService | None = None
    queries: QueryExecutionService | None = None

    def kwargs(self) -> dict[str, Any]:
        return {
            "artifacts": self.artifacts,
            "retriever": self.retriever,
            "discovery": self.discovery,
            "queries": self.queries,
        }


def investigation_wiring(
    settings: BackendSettings, persistence: Persistence, access: AccessServices
) -> InvestigationWiring:
    """Fixture mode: no warehouse tools. Live mode: discovery and guarded
    BigQuery queries with run budgets. Both: Golden methods, reports."""
    artifacts = build_artifacts(settings, persistence)
    retriever = build_retrieval(
        settings, build_knowledge(persistence, artifacts, access.resolver)
    )
    if settings.mode is not RuntimeMode.LIVE:
        return InvestigationWiring(artifacts, retriever)
    discovery = build_discovery(settings)
    queries = build_query_execution(
        settings,
        persistence,
        access.resolver,
        discovery,
        budgets=build_run_budgets(settings, persistence.budgets),
    )
    return InvestigationWiring(artifacts, retriever, discovery, queries)


# --- ownership ------------------------------------------------------------


def describe_foreign_work(error: IncompatibleActiveExecutions) -> str:
    foreign = error.foreign
    runs = ", ".join(foreign.sample_run_ids)
    more = foreign.active_runs - len(foreign.sample_run_ids)
    listing = (
        f" (runs: {runs}{f' and {more} more' if more > 0 else ''})" if runs else ""
    )
    what = (
        f"{foreign.active_runs} active investigation(s){listing} and "
        f"{foreign.queued_requests} queued request(s)"
    )
    if foreign.backend is ExecutionBackend.TEMPORAL:
        return (
            f"{what} were started with Temporal execution, which local execution "
            "does not take over. Either let them finish or cancel them with "
            "Temporal first (./scripts/dev.sh --execution-backend temporal, then "
            "`analytics cancel RUN_ID` as their owner), or keep using Temporal: "
            f"set {BACKEND_VARIABLE}=temporal in the environment file. Nothing "
            "was changed."
        )
    return (
        f"{what} belong to local execution, which Temporal does not take over. "
        "If an API with local execution is still running, stop it and let its "
        "work end first; if it already stopped, start once with local execution "
        "(./scripts/dev.sh --execution-backend local) so they are marked "
        "interrupted, then switch. Nothing was changed."
    )


async def ensure_owned_work(
    persistence: Persistence, backend: ExecutionBackend
) -> None:
    """Refuse to start ``backend`` while the other one owns unfinished work."""
    ownership = ExecutionOwnership(
        PostgresActiveExecutions(Database(persistence.engine))
    )
    try:
        await ownership.ensure_no_foreign_work(backend)
    except IncompatibleActiveExecutions as error:
        raise ExecutionStartupError(describe_foreign_work(error)) from None


# --- local backend --------------------------------------------------------


@asynccontextmanager
async def local_scheduler(
    settings: BackendSettings,
    persistence: Persistence,
    *,
    model: InvestigationModel | None = None,
    registry: CapabilityRegistry | None = None,
) -> AsyncIterator[LocalInvestigationManager]:
    """The opened local manager hosting this process's investigations.

    Takes the one-manager-per-database lock (``ExecutionStartupError`` if
    another process holds it) and ends orphaned local runs as interrupted
    before admitting work; on exit, ends owned work within the configured
    grace. ``model``/``registry`` replace the configured ones (tests).
    """
    access = build_access(persistence, local_token_authority(settings))
    wiring = investigation_wiring(settings, persistence, access)
    local = build_local_investigations(
        settings,
        persistence,
        access,
        model if model is not None else investigation_model(settings),
        max_concurrent=settings.local_max_concurrent_runs,
        shutdown_grace=timedelta(seconds=settings.local_shutdown_grace_seconds),
        registry=registry,
        **wiring.kwargs(),
    )
    try:
        await local.manager.open()
    except ManagerLockHeld:
        raise ExecutionStartupError(LOCK_HELD_MESSAGE) from None
    try:
        yield local.manager
    finally:
        await local.manager.close()
