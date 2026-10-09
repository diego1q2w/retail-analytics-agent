"""Composition root for the HTTP backend (``retail-analytics-api``).

The API needs PostgreSQL, Temporal (to schedule investigations; connected
lazily, so the API can start first) and the token signing key, in every mode:
no route skips authentication. Investigations themselves run in
``retail-analytics-worker``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import click
import uvicorn
from fastapi import FastAPI
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from temporalio.client import Client

from retail_analytics import __version__
from retail_analytics.adapters.postgres.conversations import PostgresConversationReader
from retail_analytics.adapters.postgres.database import Database
from retail_analytics.adapters.postgres.investigations import (
    PostgresInvestigationInputs,
    PostgresRunPrincipals,
)
from retail_analytics.adapters.temporal.scheduler import TemporalInvestigationScheduler
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
from retail_analytics.bootstrap.persistence import (
    Persistence,
    persistence_from_settings,
)
from retail_analytics.bootstrap.persona import build_persona
from retail_analytics.bootstrap.preferences import build_preferences
from retail_analytics.bootstrap.report_deletion import build_report_deletion
from retail_analytics.bootstrap.reports import build_reports
from retail_analytics.interfaces.http.app import create_app
from retail_analytics.interfaces.http.services import HttpServices, StreamSettings

API_REQUIRED_SETTINGS = ("database_url", "temporal_address", "auth_signing_key")


def require_api_settings(settings: BackendSettings) -> None:
    missing = [
        "RETAIL_ANALYTICS_" + name.upper()
        for name in API_REQUIRED_SETTINGS
        if getattr(settings, name) is None
    ]
    if missing:
        raise ConfigError([f"{name}: required by the HTTP API" for name in missing])


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
        ),
        investigations=control,
        reports=build_reports(
            persistence, artifacts.service, evidence, context.gate, access.resolver
        ),
        deletions=build_report_deletion(persistence, access.resolver),
        persona=build_persona(persistence, access.resolver).service,
    )


def build_app(
    settings: BackendSettings, stream: StreamSettings | None = None
) -> FastAPI:
    @asynccontextmanager
    async def services() -> AsyncIterator[HttpServices]:
        require_api_settings(settings)
        client = await Client.connect(
            settings.temporal_address or "",
            namespace=settings.temporal_namespace,
            plugins=[PydanticAIPlugin()],
            lazy=True,
        )
        persistence = persistence_from_settings(settings)
        try:
            scheduler = TemporalInvestigationScheduler(
                client, settings.temporal_task_queue
            )
            yield build_http_services(settings, persistence, scheduler)
        finally:
            persistence.close()

    return create_app(
        mode=settings.mode.value,
        version=__version__,
        services=services,
        stream=stream,
    )


@click.command()
@click.option("--check-config", is_flag=True, help="Validate settings and exit.")
def main(check_config: bool) -> None:
    """Run the HTTP backend (needs PostgreSQL, Temporal and the signing key)."""
    settings = settings_or_exit(check_config)
    try:
        require_api_settings(settings)
    except ConfigError as error:
        click.echo(str(error), err=True)
        raise SystemExit(CONFIG_ERROR_EXIT_CODE) from None
    uvicorn.run(build_app(settings), host=settings.api_host, port=settings.api_port)


if __name__ == "__main__":
    main()
