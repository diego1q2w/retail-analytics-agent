"""Composition of Golden Knowledge: PostgreSQL repository plus the artifact store."""

from __future__ import annotations

from dataclasses import dataclass

from retail_analytics.adapters.postgres.database import Database
from retail_analytics.adapters.postgres.embeddings import PostgresEmbeddingStore
from retail_analytics.adapters.postgres.knowledge import PostgresKnowledgeRepository
from retail_analytics.application.authorization import AccessResolver
from retail_analytics.application.knowledge import (
    GoldenKnowledgeReader,
    KnowledgeService,
)
from retail_analytics.application.ports.knowledge import (
    KnowledgeIndexSource,
    KnowledgeRepository,
)
from retail_analytics.application.ports.retrieval import EmbeddingStore
from retail_analytics.bootstrap.artifacts import ArtifactServices
from retail_analytics.bootstrap.persistence import Persistence


@dataclass(frozen=True)
class KnowledgeServices:
    service: KnowledgeService
    reader: GoldenKnowledgeReader
    index_source: KnowledgeIndexSource
    # Trusted maintenance and tests only; models never get repository access.
    repository: KnowledgeRepository
    # Durable vector cache for retrieval (None in memory-only test wiring).
    embeddings: EmbeddingStore | None = None


def build_knowledge(
    persistence: Persistence,
    artifacts: ArtifactServices,
    resolver: AccessResolver,
    *,
    self_publishers: frozenset[str] = frozenset(),
) -> KnowledgeServices:
    """``self_publishers`` stays empty except in the local development-admin
    command (``bootstrap.knowledge_admin``)."""
    db = Database(persistence.engine)
    repository = PostgresKnowledgeRepository(db)
    return KnowledgeServices(
        service=KnowledgeService(
            resolver,
            repository,
            artifacts.service,
            artifacts.maintenance,
            self_publishers=self_publishers,
        ),
        reader=GoldenKnowledgeReader(repository, artifacts.service),
        index_source=repository,
        repository=repository,
        embeddings=PostgresEmbeddingStore(db),
    )
