"""Composition of artifact storage: local blob directory plus PostgreSQL metadata."""

from __future__ import annotations

from dataclasses import dataclass

from retail_analytics.adapters.filesystem.blobs import LocalBlobStore
from retail_analytics.adapters.postgres.artifacts import PostgresArtifactCatalog
from retail_analytics.adapters.postgres.database import Database
from retail_analytics.application.artifacts import (
    ArtifactMaintenance,
    ArtifactPolicy,
    ArtifactService,
)
from retail_analytics.bootstrap.config import BackendSettings
from retail_analytics.bootstrap.persistence import Persistence


@dataclass(frozen=True)
class ArtifactServices:
    service: ArtifactService
    maintenance: ArtifactMaintenance


def build_artifacts(
    settings: BackendSettings, persistence: Persistence
) -> ArtifactServices:
    catalog = PostgresArtifactCatalog(Database(persistence.engine))
    blobs = LocalBlobStore(settings.artifact_dir)
    policy = ArtifactPolicy.with_limits(
        markdown=settings.artifact_max_markdown_bytes,
        binary=settings.artifact_max_binary_bytes,
    )
    return ArtifactServices(
        service=ArtifactService(catalog, blobs, policy),
        maintenance=ArtifactMaintenance(catalog, blobs),
    )
