"""Golden Knowledge against real PostgreSQL (needs Docker).

Runs the same scenarios as the in-memory unit tests, then checks what only the
database can: constraints, concurrency, append-only history and the schema.
"""

from __future__ import annotations

import asyncio
import inspect
import uuid
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path

import psycopg
import pytest
import pytest_asyncio

from retail_analytics.adapters.auth.local_jwt import LocalJwtAuthority
from retail_analytics.application.artifacts import (
    ArtifactMaintenance,
    ArtifactPolicy,
    ArtifactService,
)
from retail_analytics.application.contracts.authorization import ExecutiveRegistration
from retail_analytics.application.knowledge import KnowledgeError
from retail_analytics.bootstrap.access import build_access
from retail_analytics.bootstrap.artifacts import ArtifactServices
from retail_analytics.bootstrap.knowledge import build_knowledge
from retail_analytics.bootstrap.persistence import Persistence, build_persistence
from retail_analytics.domain.errors import InvalidTransition
from retail_analytics.domain.knowledge import ErasureReason, ReviewStatus
from tests.integration.compose_stack import Stack, running_stack
from tests.knowledge_scenarios import OK, SCENARIOS, Harness, draft, publish, submit

pytestmark = [pytest.mark.docker, pytest.mark.asyncio]
ISSUER = "retail-analytics-local"


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


async def _register(db: Persistence, roles: set[str], products: set[str]) -> str:
    from retail_analytics.domain.access import Role

    executive_id = f"exec-{uuid.uuid4().hex[:12]}"
    await db.access_admin.register_executive(
        ExecutiveRegistration(
            executive_id=executive_id,
            issuer=ISSUER,
            subject=f"sub-{executive_id}",
            roles=frozenset(Role(r) for r in roles),
            label="Test executive",
        )
    )
    await db.access_admin.replace_products(executive_id, products)
    return executive_id


@pytest_asyncio.fixture
async def harness(db: Persistence, tmp_path: Path) -> Harness:
    ids = {
        "author": await _register(db, {"executive"}, {"1", "2"}),
        "outsider": await _register(db, {"executive"}, {"9"}),
        "reviewer": await _register(db, {"reviewer"}, {"1", "2", "3"}),
        "blind_reviewer": await _register(db, {"reviewer"}, set()),
        "author_reviewer": await _register(db, {"executive", "reviewer"}, {"1"}),
    }
    access = build_access(
        db, LocalJwtAuthority("k" * 48, issuer=ISSUER, audience="retail-analytics-api")
    )
    service, maintenance = _artifacts(db, tmp_path)
    knowledge = build_knowledge(
        db, ArtifactServices(service, maintenance), access.resolver
    )
    return Harness(
        service=knowledge.service,
        reader=knowledge.reader,
        index_source=knowledge.index_source,
        repository=knowledge.repository,
        artifacts=service,
        maintenance=maintenance,
        ids=ids,
    )


def _artifacts(
    db: Persistence, root: Path
) -> tuple[ArtifactService, ArtifactMaintenance]:
    from retail_analytics.adapters.filesystem.blobs import LocalBlobStore
    from retail_analytics.adapters.postgres.artifacts import PostgresArtifactCatalog
    from retail_analytics.adapters.postgres.database import Database

    catalog = PostgresArtifactCatalog(Database(db.engine))
    blobs = LocalBlobStore(root)
    return (
        ArtifactService(catalog, blobs, ArtifactPolicy()),
        ArtifactMaintenance(catalog, blobs),
    )


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.__name__)
async def test_scenarios_on_postgres(
    scenario: Callable[..., Awaitable[None]],
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    params = inspect.signature(scenario).parameters
    await scenario(*((harness, monkeypatch) if "monkeypatch" in params else (harness,)))


async def test_concurrent_approvals_publish_once_and_history_is_append_only(
    stack: Stack, harness: Harness
) -> None:
    v = await submit(harness)
    outcomes = await asyncio.gather(
        *(
            harness.service.approve(
                harness.as_("reviewer"), v.ref, rationale="ok", checks=OK
            )
            for _ in range(4)
        ),
        return_exceptions=True,
    )
    wins = [o for o in outcomes if not isinstance(o, BaseException)]
    assert len(wins) == 1
    losers = [o for o in outcomes if isinstance(o, BaseException)]
    assert len(losers) == 3
    assert all(isinstance(o, KnowledgeError | InvalidTransition) for o in losers)
    with psycopg.connect(stack.app_dsn) as conn:
        published = conn.execute(
            "select count(*) from golden_versions where example_id = %s "
            "and status = 'published'",
            (v.example_id,),
        ).fetchone()
        assert published == (1,)
        with pytest.raises(psycopg.errors.RestrictViolation):
            conn.execute("update golden_review_events set rationale = 'x'")
        conn.rollback()
        with pytest.raises(psycopg.errors.RestrictViolation):
            conn.execute("update golden_index_events set reason = 'x'")


async def test_database_holds_no_content_after_erasure_and_enforces_one_published(
    stack: Stack, harness: Harness
) -> None:
    secret = "Which customers bought the Zebra Parka?"
    v = await publish(harness, await submit(harness, draft(question=secret)))
    await harness.service.erase(
        harness.as_("reviewer"), v.ref, reason=ErasureReason.PRIVACY_REQUEST
    )
    with psycopg.connect(stack.app_dsn) as conn:
        rows = conn.execute(
            "select (select coalesce(string_agg(t::text, ' '), '') "
            "from golden_versions t where example_id = %s) || ' ' || "
            "(select coalesce(string_agg(t::text, ' '), '') "
            "from golden_review_events t where example_id = %s) || ' ' || "
            "(select coalesce(string_agg(t::text, ' '), '') "
            "from golden_index_events t where example_id = %s)",
            (v.example_id,) * 3,
        ).fetchone()
        assert rows is not None and "Zebra" not in rows[0]
        status = conn.execute(
            "select status, purge_artifact_id from golden_versions "
            "where example_id = %s",
            (v.example_id,),
        ).fetchone()
        assert status == (ReviewStatus.ERASED.value, None)
        a = await submit(harness)
        await publish(harness, a)
        b = await submit(harness, draft(), example_id=a.example_id)
        assert b.version == 2
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "update golden_versions set status = 'published' "
                "where example_id = %s and version = 2",
                (a.example_id,),
            )


async def test_embeddings_persist_across_restart_and_are_erased_with_the_example(
    stack: Stack, db: Persistence, harness: Harness
) -> None:
    from retail_analytics.adapters.postgres.database import Database
    from retail_analytics.adapters.postgres.embeddings import PostgresEmbeddingStore
    from retail_analytics.application.retrieval import GoldenIndex
    from tests.unit.test_retrieval import CORPUS, CountingEmbedder, _draft

    store = PostgresEmbeddingStore(Database(db.engine))
    v = await publish(harness, await submit(harness, _draft("revenue")))
    other = await publish(harness, await submit(harness, _draft("returns")))
    first = CountingEmbedder()
    await GoldenIndex(harness.index_source, first, store).sync()
    assert first.embedded >= 2

    restarted = CountingEmbedder()
    index = GoldenIndex(harness.index_source, restarted, store)
    await index.sync()
    assert restarted.embedded == 0 and index.size >= 2

    smaller = CountingEmbedder()
    smaller._dimensions = 64
    await GoldenIndex(harness.index_source, smaller, store).sync()
    assert smaller.embedded >= 2  # new model/dimension key, not a reuse

    def count(digest: str) -> int:
        with psycopg.connect(stack.app_dsn) as conn:
            row = conn.execute(
                "select count(*) from golden_embeddings where content_digest = %s",
                (digest,),
            ).fetchone()
            assert row is not None
            return int(row[0])

    digest = v.content_digest
    assert digest is not None and other.content_digest is not None
    assert count(digest) == 2 and count(other.content_digest) == 2
    await harness.service.erase(
        harness.as_("reviewer"), v.ref, reason=ErasureReason.PRIVACY_REQUEST
    )
    assert count(digest) == 0
    assert count(other.content_digest) == 2
    assert CORPUS["revenue"][0]  # corpus text never stored in the table
    with psycopg.connect(stack.app_dsn) as conn:
        cols = conn.execute(
            "select column_name from information_schema.columns "
            "where table_name = 'golden_embeddings'"
        ).fetchall()
    assert {c[0] for c in cols} == {
        "content_digest",
        "model_id",
        "dimensions",
        "vector",
        "created_at",
    }
