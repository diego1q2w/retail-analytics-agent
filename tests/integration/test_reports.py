"""Saved reports against real PostgreSQL and the local artifact store (Docker)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import psycopg
import pytest

from retail_analytics.adapters.filesystem.blobs import LocalBlobStore
from retail_analytics.adapters.postgres.artifacts import PostgresArtifactCatalog
from retail_analytics.adapters.postgres.database import Database
from retail_analytics.application.artifacts import ArtifactPolicy, ArtifactService
from retail_analytics.application.authorization import AccessDenied
from retail_analytics.application.contracts.authorization import (
    ExecutiveRegistration,
    Principal,
)
from retail_analytics.application.contracts.persistence import (
    OperationRequest,
    RunRequest,
)
from retail_analytics.application.contracts.reports import ReportAccess
from retail_analytics.application.contracts.tools import OperationContext
from retail_analytics.application.output_privacy import OutputWithheld
from retail_analytics.bootstrap.access import build_access
from retail_analytics.bootstrap.context import build_context
from retail_analytics.bootstrap.evidence import build_evidence
from retail_analytics.bootstrap.persistence import Persistence, build_persistence
from retail_analytics.bootstrap.preferences import build_preferences
from retail_analytics.bootstrap.reports import build_reports
from retail_analytics.domain.access import Permission, Role
from retail_analytics.domain.evidence import (
    AnalysisStamp,
    EvidenceContent,
    EvidenceKind,
    EvidenceTable,
    Provenance,
)
from retail_analytics.domain.operations import SideEffect
from retail_analytics.domain.periods import DateWindow
from retail_analytics.domain.reports import ReportError, ReportErrorCode
from retail_analytics.domain.runs import RunStatus
from tests.integration.compose_stack import Stack, running_stack
from tests.unit.evidence.support import FINGERPRINT, REVENUE
from tests.unit.reports.support import PRODUCT_COLUMNS, PRODUCT_ROWS, draft

pytestmark = [pytest.mark.docker, pytest.mark.asyncio]
SCOPES = frozenset(p.value for p in Permission)


@pytest.fixture(scope="module")
def stack() -> Iterator[Stack]:
    with running_stack("postgres") as stack:
        stack.migrate()
        yield stack


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class Env:
    def __init__(self, stack: Stack, root: Path) -> None:
        self.stack = stack
        self.db: Persistence = build_persistence(stack.app_url)
        self.access = build_access(self.db, verifier=None)  # type: ignore[arg-type]
        self.evidence = build_evidence(self.db)
        context = build_context(
            self.db,
            self.access,
            self.evidence,
            build_preferences(self.db, self.access),
        )
        self.artifacts = ArtifactService(
            PostgresArtifactCatalog(Database(self.db.engine)),
            LocalBlobStore(root),
            ArtifactPolicy(),
        )
        self.reports = build_reports(
            self.db, self.artifacts, self.evidence, context.gate, self.access.resolver
        )

    async def executive(self, products: set[str]) -> tuple[Principal, str]:
        executive_id = _id("exec")
        await self.db.access_admin.register_executive(
            ExecutiveRegistration(
                executive_id=executive_id,
                issuer="iss",
                subject=f"sub-{executive_id}",
                roles=frozenset({Role.EXECUTIVE}),
                label="Test executive",
            )
        )
        await self.db.access_admin.replace_products(executive_id, products)
        session = await self.db.sessions.create_session(_id("ses"), executive_id)
        return Principal(executive_id, SCOPES), session.session_id

    async def run(self, principal: Principal, session_id: str) -> str:
        active = await self.db.runs.active_run(session_id)
        if active is not None:
            await self.db.runs.transition_run(active.run_id, RunStatus.COMPLETED)
        started = await self.db.runs.start_run(
            RunRequest(
                run_id=_id("run"),
                session_id=session_id,
                requested_by=principal.executive_id,
                submission_key=_id("key"),
                message_id=_id("msg"),
                request_text="Revenue by product",
            )
        )
        return started.run.run_id

    async def product_evidence(self, principal: Principal, run_id: str) -> str:
        ctx = await self.access.resolver.context_for_run(principal, run_id)
        op = await self.db.tool_executions.begin(
            OperationRequest(
                operation_id=_id("op"),
                run_id=run_id,
                capability="execute_analysis",
                capability_version=1,
                side_effect=SideEffect.EXTERNAL_JOB,
            )
        )
        content = EvidenceContent(
            kind=EvidenceKind.EXTERNAL,
            subject_key=_id("p"),
            analysis=AnalysisStamp(
                1,
                1,
                frozenset({REVENUE}),
                FINGERPRINT,
                period=DateWindow(date(2026, 9, 1), date(2026, 10, 1)),
            ),
            provenance=Provenance(notes=(("source", "fixture"),)),
            table=EvidenceTable(
                columns=PRODUCT_COLUMNS,
                rows=PRODUCT_ROWS,  # type: ignore[arg-type]
                received_rows=len(PRODUCT_ROWS),
            ),
            grain=("product_id",),
        )
        record = await self.evidence.record(
            OperationContext(ctx, op.execution.operation_id), content
        )
        return record.evidence_id


@pytest.fixture
def env(stack: Stack, tmp_path: Path) -> Iterator[Env]:
    e = Env(stack, tmp_path / "artifacts")
    yield e
    e.db.close()


async def test_report_lifecycle_with_real_stores(env: Env, stack: Stack) -> None:
    alice, session = await env.executive({"1", "2", "3"})
    bob, bob_session = await env.executive({"2"})
    run = await env.run(alice, session)
    evidence_id = await env.product_evidence(alice, run)

    op = _id("op")
    first = await env.reports.create(alice, run, draft(evidence_id), operation_id=op)
    again = await env.reports.create(alice, run, draft(evidence_id), operation_id=op)
    assert again.duplicate and again.version == first.version
    report_id = first.version.report_id

    second = await env.reports.create(
        alice,
        run,
        draft(evidence_id, title="September revenue, revised"),
        operation_id=_id("op"),
        report_id=report_id,
        base_version=1,
    )
    assert second.version.version == 2

    old = await env.reports.read(alice, report_id, 1)
    assert old.markdown.startswith("# September revenue by product")
    exported = await env.reports.export(alice, report_id)
    text = exported.content.decode()
    assert "| 101 | Unnamed product | Acme | 10 |" in text
    assert "| 102 | Unnamed product | Unknown brand | 20 |" in text

    assert [r.report_id for r in await env.reports.list_reports(alice)] == [report_id]
    assert [
        r.report_id for r in await env.reports.list_reports(alice, session_id=session)
    ] == [report_id]
    assert await env.reports.list_reports(alice, session_id=bob_session) == ()
    found = await env.reports.search(alice, "revised")
    assert [m.listing.version for m in found.matches] == [2]

    # Another executive: no read, no listing, no search hit, no direct artifact.
    with pytest.raises(AccessDenied):
        await env.reports.read(bob, report_id)
    assert await env.reports.list_reports(bob) == ()
    assert (await env.reports.search(bob, "revenue")).matches == ()
    with pytest.raises(AccessDenied):
        await env.artifacts.read(bob.executive_id, report_id)

    with psycopg.connect(stack.app_url.replace("+psycopg", "")) as conn:
        with pytest.raises(psycopg.Error):
            conn.execute(
                "UPDATE report_versions SET title = 'x' WHERE report_id = %s",
                (report_id,),
            )
        conn.rollback()
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute("DELETE FROM evidence WHERE evidence_id = %s", (evidence_id,))
        conn.rollback()
        conn.execute(
            "UPDATE reports SET deleted_at = now() WHERE report_id = %s", (report_id,)
        )
        conn.commit()
    assert await env.reports.list_reports(alice) == ()
    assert (await env.reports.search(alice, "revised")).matches == ()
    with pytest.raises(AccessDenied):
        await env.reports.read(alice, report_id)


async def test_concurrent_duplicate_saves_create_one_report(env: Env) -> None:
    alice, session = await env.executive({"1", "2", "3"})
    run = await env.run(alice, session)
    evidence_id = await env.product_evidence(alice, run)
    op = _id("op")
    results = await asyncio.gather(
        *(
            env.reports.create(alice, run, draft(evidence_id), operation_id=op)
            for _ in range(5)
        )
    )
    assert len({r.version.report_id for r in results}) == 1
    assert sum(not r.duplicate for r in results) == 1
    assert len(await env.reports.list_reports(alice)) == 1
    assert len(await env.reports.versions(alice, results[0].version.report_id)) == 1


async def test_access_change_discards_new_reports_and_hides_saved_ones(
    env: Env,
) -> None:
    alice, session = await env.executive({"1", "2", "3"})
    run = await env.run(alice, session)
    evidence_id = await env.product_evidence(alice, run)
    saved = await env.reports.create(
        alice, run, draft(evidence_id), operation_id=_id("op")
    )
    report_id = saved.version.report_id

    await env.db.access_admin.replace_products(alice.executive_id, {"1", "3"})
    with pytest.raises(ReportError) as read:
        await env.reports.read(alice, report_id)
    assert read.value.code is ReportErrorCode.ACCESS_CHANGED
    (listing,) = await env.reports.list_reports(alice)
    assert listing.title is None and listing.access is ReportAccess.ACCESS_CHANGED
    assert (await env.reports.search(alice, "September")).matches == ()
    with pytest.raises(OutputWithheld):
        await env.reports.create(
            alice,
            run,
            draft(evidence_id, title="Too late"),
            operation_id=_id("op"),
        )
    assert len(await env.reports.versions(alice, report_id)) == 1

    await env.db.access_admin.replace_products(alice.executive_id, {"1", "2", "3"})
    assert (await env.reports.read(alice, report_id)).version.version == 1
