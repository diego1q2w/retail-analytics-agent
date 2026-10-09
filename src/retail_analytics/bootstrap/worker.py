"""Composition root for the Temporal investigation worker.

Used only with ``RETAIL_ANALYTICS_EXECUTION_BACKEND=temporal``. With local
execution (the default) investigations run inside ``retail-analytics-api``,
so this command exits at once with an instruction instead of starting
duplicate work.
"""

from __future__ import annotations

import asyncio

import click

from retail_analytics.adapters.postgres.database import Database
from retail_analytics.adapters.postgres.investigation_recovery import (
    PostgresRecoveryCandidates,
)
from retail_analytics.application.investigation_recovery import InvestigationRecovery
from retail_analytics.bootstrap.access import build_access, local_token_authority
from retail_analytics.bootstrap.config import BackendSettings, ConfigError
from retail_analytics.bootstrap.entrypoint import (
    CONFIG_ERROR_EXIT_CODE,
    settings_or_exit,
)
from retail_analytics.bootstrap.execution import (
    WORKER_NOT_USED_MESSAGE,
    investigation_model,
    investigation_wiring,
)
from retail_analytics.bootstrap.investigations import build_investigations
from retail_analytics.bootstrap.persistence import persistence_from_settings
from retail_analytics.bootstrap.telemetry import install_from_settings
from retail_analytics.bootstrap.temporal import (
    connect,
    investigation_worker,
)
from retail_analytics.bootstrap.temporal import (
    scheduler as temporal_scheduler,
)
from retail_analytics.domain.runs import ExecutionBackend

# Exit status when the worker is not part of the selected execution backend.
NOT_USED_EXIT_CODE = 3


async def run_worker(settings: BackendSettings) -> None:
    if settings.temporal_address is None:
        raise ConfigError(["RETAIL_ANALYTICS_TEMPORAL_ADDRESS: required for worker"])
    install_from_settings(settings, "worker")
    # Fail on configuration problems before connecting to anything.
    model = investigation_model(settings)
    client = await connect(settings.temporal_address, settings.temporal_namespace)
    persistence = persistence_from_settings(settings)
    try:
        access = build_access(persistence, local_token_authority(settings))
        scheduler = temporal_scheduler(client, settings.temporal_task_queue)
        services = build_investigations(
            settings,
            persistence,
            access,
            scheduler,
            model,
            **investigation_wiring(settings, persistence, access).kwargs(),
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

        async with investigation_worker(client, settings.temporal_task_queue, services):
            click.echo("investigation worker ready", err=True)
            await dispatch()
    finally:
        persistence.close()


@click.command()
@click.option("--check-config", is_flag=True, help="Validate settings and exit.")
def main(check_config: bool) -> None:
    """Run the Temporal worker (only with Temporal execution).

    Fixture mode uses the offline model and no warehouse tools; live mode
    uses the Gemini/GPT provider chain with discovery and query tools. Both
    register Golden methods, preferences, reports, deletion proposals and
    currency conversion. With local execution it exits with status 3.
    """
    settings = settings_or_exit(check_config)
    if settings.execution_backend is not ExecutionBackend.TEMPORAL:
        click.echo(WORKER_NOT_USED_MESSAGE, err=True)
        raise SystemExit(NOT_USED_EXIT_CODE)
    try:
        asyncio.run(run_worker(settings))
    except ConfigError as error:
        click.echo(str(error), err=True)
        raise SystemExit(CONFIG_ERROR_EXIT_CODE) from None
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
