"""Composition root for the Temporal investigation worker."""

from __future__ import annotations

import asyncio

import click
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from temporalio.client import Client
from temporalio.worker import Worker

from retail_analytics.adapters.models.fixture import fixture_model
from retail_analytics.adapters.postgres.database import Database
from retail_analytics.adapters.postgres.investigation_recovery import (
    PostgresRecoveryCandidates,
)
from retail_analytics.adapters.temporal.activities import REGISTERED
from retail_analytics.adapters.temporal.scheduler import TemporalInvestigationScheduler
from retail_analytics.adapters.temporal.workflow import InvestigationWorkflow
from retail_analytics.application.investigation_recovery import InvestigationRecovery
from retail_analytics.bootstrap.access import build_access, local_token_authority
from retail_analytics.bootstrap.config import BackendSettings, ConfigError, RuntimeMode
from retail_analytics.bootstrap.entrypoint import settings_or_exit
from retail_analytics.bootstrap.investigations import build_investigations
from retail_analytics.bootstrap.persistence import persistence_from_settings


async def run_worker(settings: BackendSettings) -> None:
    if settings.temporal_address is None:
        raise ConfigError(["RETAIL_ANALYTICS_TEMPORAL_ADDRESS: required for worker"])
    if settings.mode is not RuntimeMode.FIXTURE:
        raise ConfigError(["live model wiring requires the provider integration task"])
    client = await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
        plugins=[PydanticAIPlugin()],
    )
    persistence = persistence_from_settings(settings)
    try:
        access = build_access(persistence, local_token_authority(settings))
        scheduler = TemporalInvestigationScheduler(client, settings.temporal_task_queue)
        services = build_investigations(
            settings, persistence, access, scheduler, fixture_model()
        )
        recovery = InvestigationRecovery(
            PostgresRecoveryCandidates(Database(persistence.engine)),
            services.launcher,
            services.inputs,
            scheduler,
        )

        async def dispatch() -> None:
            while True:
                try:
                    await recovery.dispatch()
                except Exception:
                    # No input, SQL, provider errors or credentials in logs.
                    click.echo(
                        "investigation notification recovery will retry", err=True
                    )
                await asyncio.sleep(2)

        async with Worker(
            client,
            task_queue=settings.temporal_task_queue,
            workflows=[InvestigationWorkflow],
            activities=REGISTERED,
        ):
            click.echo("investigation worker ready", err=True)
            await dispatch()
    finally:
        persistence.close()


@click.command()
@click.option("--check-config", is_flag=True, help="Validate settings and exit.")
def main(check_config: bool) -> None:
    """Run the durable-execution worker (fixture model in this release)."""
    settings = settings_or_exit(check_config)
    try:
        asyncio.run(run_worker(settings))
    except ConfigError as error:
        click.echo(str(error), err=True)
        raise SystemExit(2) from None
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
