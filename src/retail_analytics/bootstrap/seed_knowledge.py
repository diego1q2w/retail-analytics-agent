"""Development command: ``python -m retail_analytics.bootstrap.seed_knowledge``.

Loads the project-authored Golden seed library into the local database as
``demo-a`` (author) and publishes it as ``demo-b`` (independent reviewer).
Needs the migrated database and the demo executives from
``retail-analytics-dev-access provision``. Safe to rerun.
"""

from __future__ import annotations

import asyncio

import click

from retail_analytics.application.authorization import (
    AccessDenied,
    AccessResolver,
    OwnershipGuard,
)
from retail_analytics.application.golden_seeding import (
    SeedLibraryInvalid,
    seed_golden_library,
    seed_principals,
)
from retail_analytics.application.knowledge import KnowledgeError
from retail_analytics.bootstrap.artifacts import build_artifacts
from retail_analytics.bootstrap.config import ConfigError, load_backend_settings
from retail_analytics.bootstrap.dev_access import DEMO_EXECUTIVES
from retail_analytics.bootstrap.entrypoint import CONFIG_ERROR_EXIT_CODE
from retail_analytics.bootstrap.knowledge import build_knowledge
from retail_analytics.bootstrap.persistence import persistence_from_settings

AUTHOR_KEY = "demo-a"
REVIEWER_KEY = "demo-b"


def _executive_id(key: str) -> str:
    return next(d.executive_id for d in DEMO_EXECUTIVES if d.key == key)


@click.command()
def main() -> None:
    """Seed and publish the Golden example library (idempotent)."""
    try:
        settings = load_backend_settings()
        persistence = persistence_from_settings(settings)
    except ConfigError as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(CONFIG_ERROR_EXIT_CODE) from None
    try:
        guard = OwnershipGuard(
            persistence.sessions, persistence.runs, persistence.tool_executions
        )
        resolver = AccessResolver(persistence.executives, guard)
        services = build_knowledge(
            persistence, build_artifacts(settings, persistence), resolver
        )
        author, reviewer = seed_principals(
            _executive_id(AUTHOR_KEY), _executive_id(REVIEWER_KEY)
        )
        try:
            results = asyncio.run(
                seed_golden_library(services.service, author, reviewer)
            )
        except SeedLibraryInvalid as exc:
            raise click.ClickException(str(exc)) from None
        except AccessDenied:
            raise click.ClickException(
                "the demo executives are not provisioned or lack a role; run "
                "retail-analytics-dev-access provision first"
            ) from None
        except KnowledgeError as exc:
            raise click.ClickException(f"seeding stopped: {exc.code.value}") from None
    finally:
        persistence.close()
    for result in results:
        click.echo(
            f"{result.key}: {result.outcome.value}"
            + (f" (status {result.status.value})" if result.status else "")
        )


if __name__ == "__main__":
    main()
