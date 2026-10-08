"""Composition root for the Temporal worker (``retail-analytics-worker``)."""

from __future__ import annotations

import click

from retail_analytics.bootstrap.entrypoint import settings_or_exit


@click.command()
@click.option("--check-config", is_flag=True, help="Validate settings and exit.")
def main(check_config: bool) -> None:
    """Run the durable-execution worker."""
    settings_or_exit(check_config)
    # Workflows and activities are registered by later tasks; until then the
    # worker has nothing to poll, so it exits instead of idling on Temporal.
    click.echo("no workflows registered yet; worker not started", err=True)


if __name__ == "__main__":
    main()
