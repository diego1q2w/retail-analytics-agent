"""Local assembly: investigations executed in-process by the API.

Explicit composition of the local backend (``adapters.local``) over the
general, runtime-neutral services of ``bootstrap.investigations``. Nothing here
imports or connects to Temporal. Runtime selection, settings and API startup
wiring are not chosen here: callers construct this explicitly.

    local = build_local_investigations(settings, persistence, access, model)
    async with local.manager:      # lock, orphan sweep, admit ... shutdown
        control = local.services.control

``local.manager`` is the ``InvestigationScheduler`` to hand to any other
composition (for example ``bootstrap.api.build_http_services``) in the same
process; only one opened manager may use a database at a time.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from pydantic_ai.models import Model

from retail_analytics.adapters.agent.investigator import (
    AgentBinding,
    build_investigation_agent,
)
from retail_analytics.adapters.local.investigations import (
    DEFAULT_MAX_CONCURRENT,
    DEFAULT_SHUTDOWN_GRACE,
    LocalInvestigationManager,
)
from retail_analytics.adapters.postgres.database import Database
from retail_analytics.adapters.postgres.local_execution import (
    PostgresManagerLock,
    PostgresOrphanedLocalRuns,
)
from retail_analytics.application.budgets import RunBudgets
from retail_analytics.application.discovery import DiscoveryService
from retail_analytics.application.investigation_interruption import (
    InterruptedRunSweep,
)
from retail_analytics.application.ports.currency_conversion import ExchangeRateProvider
from retail_analytics.application.query_execution import QueryExecutionService
from retail_analytics.application.retrieval import GoldenRetriever
from retail_analytics.application.tools import CapabilityRegistry
from retail_analytics.bootstrap.access import AccessServices
from retail_analytics.bootstrap.artifacts import ArtifactServices
from retail_analytics.bootstrap.config import BackendSettings
from retail_analytics.bootstrap.investigations import (
    InvestigationServices,
    build_investigations,
)
from retail_analytics.bootstrap.persistence import Persistence


@dataclass(frozen=True)
class LocalInvestigations:
    services: InvestigationServices
    manager: LocalInvestigationManager


def build_local_investigations(
    settings: BackendSettings,
    persistence: Persistence,
    access: AccessServices,
    model: Model | Callable[[RunBudgets], Model],
    *,
    max_concurrent: int = DEFAULT_MAX_CONCURRENT,
    shutdown_grace: timedelta = DEFAULT_SHUTDOWN_GRACE,
    discovery: DiscoveryService | None = None,
    queries: QueryExecutionService | None = None,
    registry: CapabilityRegistry | None = None,
    artifacts: ArtifactServices | None = None,
    retriever: GoldenRetriever | None = None,
    exchange_rates: ExchangeRateProvider | None = None,
) -> LocalInvestigations:
    """The general investigation services with the local manager as their
    scheduler, bound to the shared agent. Not opened: use ``async with
    local.manager`` (or ``open``/``close``) in the owning lifespan."""
    manager = LocalInvestigationManager(
        runs=persistence.runs,
        lock=PostgresManagerLock(persistence.engine),
        max_concurrent=max_concurrent,
        shutdown_grace=shutdown_grace,
    )
    services = build_investigations(
        settings,
        persistence,
        access,
        manager,
        model,
        discovery=discovery,
        queries=queries,
        registry=registry,
        artifacts=artifacts,
        retriever=retriever,
        exchange_rates=exchange_rates,
    )
    manager.bind(
        runtime=services.runtime,
        agent=build_investigation_agent(AgentBinding(services.agent)),
        sweep=InterruptedRunSweep(
            orphans=PostgresOrphanedLocalRuns(Database(persistence.engine)),
            runtime=services.runtime,
            inputs=services.inputs,
            sessions=persistence.sessions,
        ),
    )
    return LocalInvestigations(services, manager)
