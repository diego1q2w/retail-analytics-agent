"""Composition root for the HTTP backend (``retail-analytics-api``).

The API needs PostgreSQL and the token signing key in every mode: no route
skips authentication. Where investigations execute follows
``EXECUTION_BACKEND`` (``bootstrap.execution``):

- ``local`` (default): this process hosts the local manager, opened in the
  lifespan (``async with``); no Temporal is imported or contacted.
- ``temporal`` (opt-in): the API only schedules workflows (Temporal client
  connected lazily) and ``retail-analytics-worker`` executes them. The
  Temporal composition is imported only when this backend is selected.

Startup fails clearly when another local-execution API holds the database or
the other backend still owns unfinished work. ``build_http_services`` takes
any ``InvestigationScheduler``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import click
import uvicorn
from fastapi import FastAPI

from retail_analytics import __version__
from retail_analytics.adapters.postgres.conversations import PostgresConversationReader
from retail_analytics.adapters.postgres.database import Database
from retail_analytics.adapters.postgres.investigations import (
    PostgresInvestigationInputs,
    PostgresRunPrincipals,
)
from retail_analytics.application.citations import CitationSources
from retail_analytics.application.conversations import ConversationService
from retail_analytics.application.investigations import (
    InvestigationControl,
    InvestigationLauncher,
)
from retail_analytics.application.ports.investigations import InvestigationScheduler
from retail_analytics.bootstrap.access import build_access, local_token_authority
from retail_analytics.bootstrap.artifacts import build_artifacts
from retail_analytics.bootstrap.config import BackendSettings, ConfigError
from retail_analytics.bootstrap.context import build_context
from retail_analytics.bootstrap.entrypoint import (
    CONFIG_ERROR_EXIT_CODE,
    settings_or_exit,
)
from retail_analytics.bootstrap.evidence import build_evidence
from retail_analytics.bootstrap.execution import (
    ExecutionStartupError,
    ensure_owned_work,
    local_scheduler,
    require_api_settings,
)
from retail_analytics.bootstrap.models import pricing_summary, provider_summary
from retail_analytics.bootstrap.persistence import (
    Persistence,
    persistence_from_settings,
)
from retail_analytics.bootstrap.persona import build_persona
from retail_analytics.bootstrap.preferences import build_preferences
from retail_analytics.bootstrap.report_deletion import build_report_deletion
from retail_analytics.bootstrap.reports import build_reports
from retail_analytics.bootstrap.telemetry import install_from_settings
from retail_analytics.domain.runs import ExecutionBackend
from retail_analytics.interfaces.http.app import create_app
from retail_analytics.interfaces.http.services import HttpServices, StreamSettings

# Opens the investigation scheduler for the app's lifetime.
SchedulerProvider = Callable[
    [Persistence], AbstractAsyncContextManager[InvestigationScheduler]
]

__all__ = [
    "SchedulerProvider",
    "build_app",
    "build_http_services",
    "require_api_settings",
    "selected_scheduler",
]


def selected_scheduler(settings: BackendSettings) -> SchedulerProvider:
    """The scheduler of the configured execution backend."""
    if settings.execution_backend is ExecutionBackend.TEMPORAL:
        # Opt-in only: the local path never loads the Temporal SDK.
        from retail_analytics.bootstrap.temporal import api_scheduler

        return lambda _persistence: api_scheduler(settings)
    return lambda persistence: local_scheduler(settings, persistence)


def build_http_services(
    settings: BackendSettings,
    persistence: Persistence,
    scheduler: InvestigationScheduler,
) -> HttpServices:
    """Application services behind the routes. Does not bind the worker
    runtime: the API only records requests and schedules them."""
    access = build_access(persistence, local_token_authority(settings))
    db = Database(persistence.engine)
    principals = PostgresRunPrincipals(db)
    inputs = PostgresInvestigationInputs(db)
    launcher = InvestigationLauncher(
        runs=persistence.runs,
        principals=principals,
        inputs=inputs,
        scheduler=scheduler,
        resolver=access.resolver,
    )
    control = InvestigationControl(
        resolver=access.resolver,
        guard=access.guard,
        sessions=persistence.sessions,
        runs=persistence.runs,
        principals=principals,
        inputs=inputs,
        events=persistence.run_events,
        scheduler=scheduler,
        launcher=launcher,
    )
    evidence = build_evidence(persistence, settings=settings)
    preferences = build_preferences(persistence, access)
    context = build_context(persistence, access, evidence, preferences)
    artifacts = build_artifacts(settings, persistence)
    return HttpServices(
        authenticator=access.authenticator,
        conversations=ConversationService(
            resolver=access.resolver,
            guard=access.guard,
            sessions=persistence.sessions,
            runs=persistence.runs,
            inputs=inputs,
            events=persistence.run_events,
            reader=PostgresConversationReader(db),
            gate=context.gate,
            citations=CitationSources(
                resolver=access.resolver, evidence=evidence, gate=context.gate
            ),
        ),
        investigations=control,
        reports=build_reports(
            persistence, artifacts.service, evidence, context.gate, access.resolver
        ),
        deletions=build_report_deletion(persistence, access.resolver),
        persona=build_persona(persistence, access.resolver).service,
    )


def build_app(
    settings: BackendSettings,
    stream: StreamSettings | None = None,
    *,
    scheduler: SchedulerProvider | None = None,
) -> FastAPI:
    """The HTTP app. ``scheduler`` defaults to the configured backend's;
    tests pass one with a controlled model."""
    install_from_settings(settings, "api")

    @asynccontextmanager
    async def services() -> AsyncIterator[HttpServices]:
        require_api_settings(settings)
        provider = scheduler or selected_scheduler(settings)
        persistence = persistence_from_settings(settings)
        try:
            # Before anything starts: never take over the other backend's work.
            await ensure_owned_work(persistence, settings.execution_backend)
            async with provider(persistence) as opened:
                yield build_http_services(settings, persistence, opened)
        except ExecutionStartupError as error:
            click.echo(f"retail-analytics-api: cannot start: {error}", err=True)
            raise
        finally:
            persistence.close()

    return create_app(
        mode=settings.mode.value,
        execution_backend=settings.execution_backend.value,
        version=__version__,
        services=services,
        stream=stream,
    )


@click.command()
@click.option("--check-config", is_flag=True, help="Validate settings and exit.")
def main(check_config: bool) -> None:
    """Run the HTTP backend (needs PostgreSQL and the signing key; with
    Temporal execution also Temporal and retail-analytics-worker)."""
    settings = settings_or_exit(check_config)
    try:
        require_api_settings(settings)
    except ConfigError as error:
        click.echo(str(error), err=True)
        raise SystemExit(CONFIG_ERROR_EXIT_CODE) from None
    click.echo(f"retail-analytics-api: {provider_summary(settings)}", err=True)
    click.echo(f"retail-analytics-api: {pricing_summary(settings)}", err=True)
    uvicorn.run(build_app(settings), host=settings.api_host, port=settings.api_port)


if __name__ == "__main__":
    main()
