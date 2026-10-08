"""PostgreSQL artifact catalog with the local blob store (needs Docker)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from retail_analytics.adapters.filesystem.blobs import LocalBlobStore
from retail_analytics.adapters.postgres.artifacts import PostgresArtifactCatalog
from retail_analytics.adapters.postgres.database import Database
from retail_analytics.application.artifacts import (
    ArtifactMaintenance,
    ArtifactPolicy,
    ArtifactService,
)
from retail_analytics.application.authorization import AccessDenied
from retail_analytics.bootstrap.persistence import Persistence, build_persistence
from tests.integration.compose_stack import Stack, running_stack

pytestmark = [pytest.mark.docker, pytest.mark.asyncio]


@pytest.fixture(scope="module")
def stack() -> Iterator[Stack]:
    with running_stack("postgres") as stack:
        stack.migrate()
        yield stack


@pytest.fixture
def db(stack: Stack) -> Iterator[Persistence]:
    persistence = build_persistence(stack.app_url)
    yield persistence
    persistence.close()


def make(db: Persistence, root: Path) -> tuple[ArtifactService, ArtifactMaintenance]:
    catalog = PostgresArtifactCatalog(Database(db.engine))
    blobs = LocalBlobStore(root)
    return (
        ArtifactService(catalog, blobs, ArtifactPolicy()),
        ArtifactMaintenance(catalog, blobs),
    )


async def test_versions_ownership_idempotency_and_metadata_only(
    stack: Stack, db: Persistence, tmp_path: Path
) -> None:
    service, maintenance = make(db, tmp_path)
    body = b"# Report\n" + b"x" * 5000
    v1 = await service.save(
        "pa", media_type="text/markdown", content=body, idempotency_key="op-1"
    )
    again = await service.save(
        "pa", media_type="text/markdown", content=body, idempotency_key="op-1"
    )
    v2 = await service.save(
        "pa", media_type="text/markdown", content=b"# v2", idempotency_key="op-2",
        artifact_id=v1.artifact_id,
    )  # fmt: skip
    assert again == v1 and (v1.version, v2.version) == (1, 2)
    with pytest.raises(AccessDenied):
        await service.read("pb", v1.artifact_id)
    with pytest.raises(AccessDenied):
        await service.save(
            "pb", media_type="text/markdown", content=b"x", idempotency_key="z",
            artifact_id=v1.artifact_id,
        )  # fmt: skip
    # the bytes are in the directory, never in PostgreSQL
    with psycopg.connect(stack.app_dsn) as conn:
        row = conn.execute(
            "SELECT row_to_json(a)::text FROM artifact_versions a WHERE version = 1 "
            "AND artifact_id = %s",
            (v1.artifact_id,),
        ).fetchone()
        assert row is not None and "# Report" not in row[0]
        with pytest.raises(psycopg.Error):
            conn.execute("UPDATE artifact_versions SET owner_id = 'pb'")
    # a new service instance over the same directory and database still reads it
    restarted, _ = make(db, tmp_path)
    assert (await restarted.read("pa", v1.artifact_id, 1)).content == body
    assert await maintenance.purge(v1.artifact_id) == 2


async def test_concurrent_same_key_creates_one_version(
    db: Persistence, tmp_path: Path
) -> None:
    service, _ = make(db, tmp_path)
    results = await asyncio.gather(
        *(
            service.save(
                "pc",
                media_type="text/markdown",
                content=b"same",
                idempotency_key="race",
            )
            for _ in range(6)
        )
    )
    assert len({(r.artifact_id, r.version) for r in results}) == 1
