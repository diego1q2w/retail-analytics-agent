"""Development command: ``python -m retail_analytics.bootstrap.warm_embeddings``.

Embeds the published Golden examples once and stores the vectors in PostgreSQL,
so API and worker processes start without calling the embedding provider.
Fixture mode (hashing embedder) needs no key. Safe to rerun: stored vectors
are reused and only missing content is embedded.
"""

from __future__ import annotations

import asyncio

import click

from retail_analytics.application.authorization import AccessResolver, OwnershipGuard
from retail_analytics.application.retrieval import GoldenIndex, RetrievalUnavailable
from retail_analytics.bootstrap.artifacts import build_artifacts
from retail_analytics.bootstrap.config import ConfigError, load_backend_settings
from retail_analytics.bootstrap.entrypoint import CONFIG_ERROR_EXIT_CODE
from retail_analytics.bootstrap.knowledge import build_knowledge
from retail_analytics.bootstrap.persistence import persistence_from_settings
from retail_analytics.bootstrap.retrieval import build_embedder


@click.command()
def main() -> None:
    """Store embeddings for every published Golden example (idempotent)."""
    try:
        settings = load_backend_settings()
        persistence = persistence_from_settings(settings)
        embedder = build_embedder(settings)
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
        index = GoldenIndex(services.index_source, embedder, services.embeddings)
        try:
            asyncio.run(index.sync())
        except RetrievalUnavailable as exc:
            cause = exc.__cause__
            raise click.ClickException(
                f"embedding warm-up failed: {type(cause or exc).__name__}"
            ) from None
    finally:
        persistence.close()
    click.echo(f"embeddings ready: {index.size} examples, model {embedder.model_id}")
