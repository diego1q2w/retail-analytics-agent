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
from retail_analytics.application.ports.retrieval import TextEmbedder
from retail_analytics.application.retrieval import GoldenIndex, RetrievalUnavailable
from retail_analytics.bootstrap.artifacts import build_artifacts
from retail_analytics.bootstrap.config import ConfigError, load_backend_settings
from retail_analytics.bootstrap.entrypoint import CONFIG_ERROR_EXIT_CODE
from retail_analytics.bootstrap.knowledge import KnowledgeServices, build_knowledge
from retail_analytics.bootstrap.persistence import persistence_from_settings
from retail_analytics.bootstrap.retrieval import build_embedder
from retail_analytics.domain.knowledge import ExampleRef


async def warm_and_verify(services: KnowledgeServices, embedder: TextEmbedder) -> int:
    if services.embeddings is None:
        raise RetrievalUnavailable("durable embedding store is required")
    index = GoldenIndex(services.index_source, embedder, services.embeddings)
    await index.sync()
    after: ExampleRef | None = None
    verified = 0
    while documents := await services.index_source.published_documents(after, 200):
        digests = [d.content_digest for d in documents]
        stored = await services.embeddings.load(
            digests, embedder.model_id, embedder.dimensions
        )
        if any(
            d not in stored or len(stored[d]) != embedder.dimensions for d in digests
        ):
            raise RetrievalUnavailable(
                "published examples are missing stored embeddings"
            )
        verified += len(documents)
        after = documents[-1].ref
    if verified == 0:
        raise RetrievalUnavailable("no published examples available to embed")
    return verified


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
        try:
            count = asyncio.run(warm_and_verify(services, embedder))
        except RetrievalUnavailable as exc:
            cause = exc.__cause__
            raise click.ClickException(
                f"embedding warm-up failed: {type(cause or exc).__name__}"
            ) from None
    finally:
        persistence.close()
    click.echo(f"embeddings ready: {count} examples, model {embedder.model_id}")


if __name__ == "__main__":
    main()
