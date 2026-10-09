"""Manual report recovery and lifecycle cleanup on real PostgreSQL (Docker).

Fake clock for deadlines; the database is real, so the locks, foreign keys
and transactions under test are the ones production uses.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from retail_analytics.adapters.filesystem.blobs import LocalBlobStore
from retail_analytics.adapters.postgres.artifacts import PostgresArtifactCatalog
from retail_analytics.adapters.postgres.database import Database
from retail_analytics.application.artifacts import (
    ArtifactMaintenance,
    ReconcileReport,
)
from retail_analytics.application.authorization import AccessDenied
from retail_analytics.application.contracts.authorization import (
    ExecutiveRegistration,
    Principal,
)
from retail_analytics.application.contracts.lifecycle import (
    MaintenanceLimits,
    MaintenanceReport,
)
from retail_analytics.application.contracts.persistence import OperationRequest
from retail_analytics.application.lifecycle import LifecycleService
from retail_analytics.bootstrap.artifacts import ArtifactServices
from retail_analytics.bootstrap.knowledge import build_knowledge
from retail_analytics.bootstrap.lifecycle import build_lifecycle
from retail_analytics.domain.access import Permission, Role
from retail_analytics.domain.executions import ToolExecutionStatus
from retail_analytics.domain.lifecycle import (
    RestoreError,
    RestoreErrorCode,
    RetentionPolicy,
)
from retail_analytics.domain.operations import SideEffect
from retail_analytics.domain.report_deletion import (
    DeletionError,
    DeletionErrorCode,
)
from retail_analytics.domain.runs import RunStatus
from tests.integration.compose_stack import Stack, running_stack
from tests.integration.test_report_deletion import World as DeletionWorld
from tests.integration.test_reports import _id
from tests.knowledge_scenarios import Harness, delivered, publish, submit
from tests.unit.reports.support import draft

pytestmark = [pytest.mark.docker, pytest.mark.asyncio]
WEEK = timedelta(days=7)


def _fixed(at: datetime) -> Callable[[], datetime]:
    return lambda: at


@pytest.fixture(scope="module")
def stack() -> Iterator[Stack]:
    with running_stack("postgres") as stack:
        stack.migrate()
        yield stack


class FailingMaintenance(ArtifactMaintenance):
    """Artifact storage that fails its first ``failures`` purges."""

    def __init__(self, real: ArtifactMaintenance, failures: int) -> None:
        self.real = real
        self.failures = failures

    async def purge(self, artifact_id: str) -> int:
        if self.failures:
            self.failures -= 1
            raise OSError("injected storage failure")
        return await self.real.purge(artifact_id)

    async def reconcile(self, grace: timedelta = timedelta(hours=1)) -> ReconcileReport:
        return await self.real.reconcile(grace)


class World(DeletionWorld):
    def __init__(self, stack: Stack, root: Path) -> None:
        super().__init__(stack, root)
        self._root = root
        catalog = PostgresArtifactCatalog(Database(self.db.engine))
        self.blobs = LocalBlobStore(root)
        self.maintenance = ArtifactMaintenance(catalog, self.blobs, clock=self.clock)
        self.services = ArtifactServices(self.artifacts, self.maintenance)
        self.lifecycle: LifecycleService = self.make_lifecycle()

    def make_lifecycle(
        self,
        maintenance: ArtifactMaintenance | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        audit_retention: timedelta | None = None,
    ) -> LifecycleService:
        services = ArtifactServices(self.artifacts, maintenance or self.maintenance)
        return build_lifecycle(
            self.db,
            self.access.resolver,
            services,
            policy=RetentionPolicy(
                audit_retention=audit_retention or timedelta(days=90)
            ),
            clock=clock or self.clock,
        )

    async def finish(self, owner: Principal, session: str) -> None:
        active = await self.db.runs.active_run(session)
        if active is not None:
            await self.db.runs.transition_run(active.run_id, RunStatus.COMPLETED)

    async def delete(self, owner: Principal, session: str, *ids: str) -> None:
        preview = await self.propose(owner, session, *ids)
        await self.deletion.confirm(owner, preview.proposal_id)

    def count(self, table: str, where: str = "true", *args: object) -> int:
        query = f"SELECT count(*) FROM {table} WHERE {where}"  # noqa: S608
        return int(str(self.sql(query, *args)[0][0]))

    def execute(self, statement: str, *args: object) -> None:
        with psycopg.connect(self.stack.app_dsn) as conn:
            conn.execute(statement, args)
            conn.commit()

    def blob_count(self) -> int:
        return len([p for p in self.blobs_dir.rglob("*") if p.is_file()])

    @property
    def blobs_dir(self) -> Path:
        return self._root

    def audit_actions_for(self, subject: str) -> list[str]:
        rows = self.sql(
            "SELECT action FROM audit_events WHERE subject_id = %s "
            "ORDER BY occurred_at",
            subject,
        )
        return [str(r[0]) for r in rows]

    async def admin(self) -> Principal:
        executive_id = _id("admin")
        await self.db.access_admin.register_executive(
            ExecutiveRegistration(
                executive_id=executive_id,
                issuer="iss",
                subject=f"sub-{executive_id}",
                roles=frozenset({Role.ADMIN}),
                label="Admin",
            )
        )
        return Principal(executive_id, frozenset(p.value for p in Permission))


@pytest.fixture
def world(stack: Stack, tmp_path: Path) -> Iterator[World]:
    root = tmp_path / "artifacts"
    w = World(stack, root)
    w.execute(
        "TRUNCATE sessions, runs, messages, tool_executions, evidence, reports, "
        "artifact_versions, deletion_proposals, audit_events CASCADE"
    )
    yield w
    w.db.close()


def _report_gone(world: World, report_id: str) -> bool:
    return (
        world.count("reports", "report_id = %s", report_id) == 0
        and world.count("report_versions", "report_id = %s", report_id) == 0
        and world.count("artifact_versions", "artifact_id = %s", report_id) == 0
        and world.count("evidence_pins", "holder_id = %s", report_id) == 0
    )


# --- manual restore ---------------------------------------------------------------


async def test_restore_before_deadline_works_and_keeps_confirmation_spent(
    world: World,
) -> None:
    alice, session = await world.executive({"1", "2", "3"})
    report = await world.report(alice, session, "Client X revenue")
    preview = await world.propose(alice, session, report)
    await world.deletion.confirm(alice, preview.proposal_id)
    assert world.live_ids(alice) == set()
    assert {r.report_id for r in await world.reports.list_reports(alice)} == set()
    assert (await world.reports.search(alice, "Client X")).matches == ()
    listed = await world.lifecycle.list_restorable(alice)
    assert [r.report_id for r in listed] == [report]

    world.clock.advance(WEEK - timedelta(seconds=1))
    restored = await world.lifecycle.restore(alice, report)
    assert restored.report_id == report
    assert world.live_ids(alice) == {report}
    assert (await world.reports.read(alice, report)) is not None
    assert [r.report_id for r in await world.reports.list_reports(alice)] == [report]

    with pytest.raises(DeletionError) as consumed:  # the confirmation stays spent
        await world.deletion.confirm(alice, preview.proposal_id)
    assert consumed.value.code is DeletionErrorCode.ALREADY_RESOLVED
    assert world.live_ids(alice) == {report}

    actions = world.audit_actions_for(report)
    assert actions == ["report.restored"]
    (details,) = world.sql(
        "SELECT details::text FROM audit_events WHERE action = 'report.restored' "
        "AND subject_id = %s",
        report,
    )
    assert "Client X" not in str(details[0])

    with pytest.raises(RestoreError) as again:
        await world.lifecycle.restore(alice, report)
    assert again.value.code is RestoreErrorCode.NOT_DELETED


async def test_restore_needs_the_owner_or_an_admin_and_an_existing_owner(
    world: World,
) -> None:
    alice, session = await world.executive({"1", "2", "3"})
    bob, _ = await world.executive({"2"})
    report = await world.report(alice, session)
    await world.delete(alice, session, report)

    with pytest.raises(RestoreError) as other:  # same answer as an unknown report
        await world.lifecycle.restore(bob, report)
    assert other.value.code is RestoreErrorCode.NOT_FOUND
    with pytest.raises(RestoreError) as unknown:
        await world.lifecycle.restore(alice, "f" * 32)
    assert unknown.value.code is RestoreErrorCode.NOT_FOUND
    assert world.live_ids(alice) == set()

    world.execute(
        "UPDATE executives SET active = false WHERE executive_id = %s",
        alice.executive_id,
    )
    admin = await world.admin()
    with pytest.raises(RestoreError) as inactive:
        await world.lifecycle.restore(admin, report)
    assert inactive.value.code is RestoreErrorCode.OWNER_UNAVAILABLE
    world.execute(
        "UPDATE executives SET active = true WHERE executive_id = %s",
        alice.executive_id,
    )

    await world.lifecycle.restore(admin, report)
    assert world.live_ids(alice) == {report}
    (details,) = world.sql(
        "SELECT details::text FROM audit_events WHERE action = 'report.restored'"
        " AND subject_id = %s",
        report,
    )
    assert "true" in str(details[0]) and admin.executive_id not in str(details[0])


async def test_after_the_deadline_restore_fails_and_cleanup_removes_everything(
    world: World,
) -> None:
    alice, session = await world.executive({"1", "2", "3"})
    report = await world.report(alice, session)
    keep = await world.report(alice, session)
    await world.delete(alice, session, report)
    pinned = world.count("evidence_pins", "holder_id = %s", report)
    assert pinned == 1 and world.blob_count() == 2

    world.clock.advance(WEEK)
    with pytest.raises(RestoreError) as late:
        await world.lifecycle.restore(alice, report)
    assert late.value.code is RestoreErrorCode.WINDOW_CLOSED

    result = await world.lifecycle.run_maintenance()
    assert result.reports_purged == 1 and result.reports_failed == 0
    assert _report_gone(world, report)
    assert world.blob_count() == 1  # only the surviving report's content
    assert world.live_ids(alice) == {keep}
    assert world.audit_actions_for(report)[-1] == "report.purged"
    (details,) = world.sql(
        "SELECT details::text FROM audit_events WHERE action = 'report.purged' "
        "AND subject_id = %s",
        report,
    )
    assert "Revenue" not in str(details[0])

    with pytest.raises(RestoreError) as gone:
        await world.lifecycle.restore(alice, report)
    assert gone.value.code is RestoreErrorCode.NOT_FOUND

    again = await world.lifecycle.run_maintenance()  # idempotent
    assert again.reports_purged == 0 and again.reports_failed == 0
    assert world.live_ids(alice) == {keep}


async def test_purge_survives_a_storage_failure_and_finishes_next_run(
    world: World,
) -> None:
    alice, session = await world.executive({"1", "2", "3"})
    report = await world.report(alice, session)
    await world.delete(alice, session, report)
    world.clock.advance(WEEK)

    flaky = FailingMaintenance(world.maintenance, failures=1)
    service = world.make_lifecycle(flaky)
    first = await service.run_maintenance()
    assert (first.reports_purged, first.reports_failed) == (0, 1)
    # Content and pins are gone; only a content-free tombstone remains.
    assert world.count("reports", "report_id = %s", report) == 1
    assert world.count("report_versions", "report_id = %s", report) == 0
    assert (
        world.count(
            "audit_events", "action = 'report.purged' AND subject_id = %s", report
        )
        == 0
    )
    with pytest.raises(RestoreError) as tombstone:
        await service.restore(alice, report)
    assert tombstone.value.code is RestoreErrorCode.PURGED

    second = await service.run_maintenance()
    assert second.reports_purged == 1
    assert _report_gone(world, report)
    assert world.blob_count() == 0
    assert world.audit_actions_for(report) == ["report.purged"]


async def test_runs_are_bounded_and_dry_run_reports_counts_only(world: World) -> None:
    alice, session = await world.executive({"1", "2", "3"})
    ids = [await world.report(alice, session, "Secret board plan") for _ in range(3)]
    await world.delete(alice, session, *ids)
    world.clock.advance(WEEK)

    dry = await world.lifecycle.run_maintenance(dry_run=True)
    assert dry.dry_run and dry.reports_purged == 3
    assert (
        world.count(
            "reports", "deleted_at IS NOT NULL AND owner_id = %s", alice.executive_id
        )
        == 3
    )
    assert "Secret" not in repr(dry)

    first = await world.lifecycle.run_maintenance(MaintenanceLimits(max_reports=2))
    assert first.reports_purged == 2 and first.more_pending
    second = await world.lifecycle.run_maintenance(MaintenanceLimits(max_reports=2))
    assert second.reports_purged == 1 and not second.more_pending
    assert all(_report_gone(world, i) for i in ids)


# --- races ---------------------------------------------------------------------------


async def test_restore_and_purge_racing_at_the_deadline_have_one_winner(
    world: World,
) -> None:
    for _ in range(6):
        alice, session = await world.executive({"1", "2", "3"})
        report = await world.report(alice, session)
        await world.delete(alice, session, report)
        deleted_at = world.sql(
            "SELECT deleted_at FROM reports WHERE report_id = %s", report
        )[0][0]
        assert isinstance(deleted_at, datetime)
        deadline = deleted_at + WEEK
        # Restore believes it is just before the deadline; purge that it is due.
        before = world.make_lifecycle(
            clock=_fixed(deadline - timedelta(microseconds=1))
        )
        after = world.make_lifecycle(clock=_fixed(deadline))
        results = await asyncio.gather(
            before.restore(alice, report),
            after.run_maintenance(),
            return_exceptions=True,
        )
        restore_result, purge_result = results
        assert isinstance(purge_result, MaintenanceReport)
        if isinstance(restore_result, BaseException):
            assert isinstance(restore_result, RestoreError)
            assert restore_result.code in {
                RestoreErrorCode.PURGED,
                RestoreErrorCode.NOT_FOUND,
                RestoreErrorCode.WINDOW_CLOSED,
            }
            assert purge_result.reports_purged >= 1
            assert _report_gone(world, report)
            assert world.audit_actions_for(report)[-1] == "report.purged"
        else:
            # The restore committed first: purge must have left the report alone.
            assert world.live_ids(alice) == {report}
            assert world.count("report_versions", "report_id = %s", report) == 1
            assert world.count("artifact_versions", "artifact_id = %s", report) == 1
            assert "report.purged" not in world.audit_actions_for(report)


# --- investigation expiry and evidence retention -------------------------------


async def test_expired_investigations_keep_pinned_evidence_and_drop_the_rest(
    world: World,
) -> None:
    alice, session = await world.executive({"1", "2", "3"})
    kept = await world.report(alice, session, "Pinned report")
    run = await world.run(alice, session)
    loose = await world.product_evidence(alice, run)
    await world.finish(alice, session)
    other, other_session = await world.executive({"2"})
    other_run = await world.run(other, other_session)
    other_evidence = await world.product_evidence(other, other_run)
    await world.finish(other, other_session)
    messages_before = world.count("messages", "session_id = %s", session)
    assert messages_before > 0

    # Not yet expired: nothing is removed.
    world.clock.advance(WEEK - timedelta(minutes=5))
    early = await world.lifecycle.run_maintenance()
    assert early.investigations.sessions_removed == 0
    assert world.count("evidence", "evidence_id = %s", loose) == 1

    world.clock.advance(timedelta(days=1))
    result = await world.lifecycle.run_maintenance()
    assert result.investigations.sessions_removed >= 1  # the unpinned session
    assert result.investigations.sessions_shell_kept >= 1

    assert world.count("evidence", "evidence_id = %s", loose) == 0
    assert world.count("evidence", "evidence_id = %s", other_evidence) == 0
    assert world.count("sessions", "session_id = %s", other_session) == 0
    # The saved report's evidence, run and operation survive, as a bare shell.
    assert world.count("evidence", "session_id = %s", session) == 1
    assert world.count("messages", "session_id = %s", session) < messages_before
    assert (
        world.count(
            "run_events",
            "run_id IN (SELECT run_id FROM runs WHERE session_id = %s)",
            session,
        )
        == 0
    )
    assert (await world.reports.read(alice, kept)) is not None
    assert world.live_ids(alice) == {kept}

    again = await world.lifecycle.run_maintenance()  # idempotent
    assert again.investigations.evidence_removed == 0
    assert world.count("evidence", "session_id = %s", session) == 1

    # Once the report is deleted and purged, its shell goes too.
    await world.delete(alice, session, kept)
    await world.finish(alice, session)
    world.clock.advance(WEEK)
    await world.lifecycle.run_maintenance()
    assert world.count("evidence", "session_id = %s", session) == 0
    assert world.count("sessions", "session_id = %s", session) == 0


async def test_unresolved_operations_keep_their_records_and_are_flagged_once(
    world: World,
) -> None:
    alice, session = await world.executive({"1", "2", "3"})
    run = await world.run(alice, session)
    op = _id("job")
    await world.db.tool_executions.begin(
        OperationRequest(
            operation_id=op,
            run_id=run,
            capability="execute_analysis",
            capability_version=1,
            side_effect=SideEffect.EXTERNAL_JOB,
        )
    )
    await world.db.tool_executions.transition(
        op, ToolExecutionStatus.SUBMITTING, attempt=1
    )
    await world.db.tool_executions.transition(
        op, ToolExecutionStatus.OUTCOME_UNKNOWN, attempt=1
    )
    await world.db.runs.transition_run(run, RunStatus.COMPLETED)

    world.clock.advance(timedelta(days=30))
    result = await world.lifecycle.run_maintenance()
    assert result.investigations.sessions_held_unresolved >= 1
    assert result.unresolved_flagged >= 1
    assert world.count("tool_executions", "operation_id = %s", op) == 1
    assert world.count("sessions", "session_id = %s", session) == 1
    assert world.audit_actions_for(op) == ["maintenance.unresolved_flagged"]
    flagged = await world.lifecycle.unresolved_operations()
    assert op in {f.operation_id for f in flagged}

    await world.lifecycle.run_maintenance()  # flagged once only
    assert world.audit_actions_for(op) == ["maintenance.unresolved_flagged"]

    await world.db.tool_executions.transition(
        op, ToolExecutionStatus.CANCELLED, attempt=1
    )
    await world.lifecycle.run_maintenance()
    assert world.count("sessions", "session_id = %s", session) == 0
    assert op not in {
        f.operation_id for f in await world.lifecycle.unresolved_operations()
    }


async def test_a_report_with_unresolved_operations_is_not_purged_until_they_settle(
    world: World,
) -> None:
    alice, session = await world.executive({"1", "2", "3"})
    report = await world.report(alice, session)
    run = world.sql("SELECT run_id FROM report_versions WHERE report_id = %s", report)[
        0
    ][0]
    op = _id("job")
    await world.db.tool_executions.begin(
        OperationRequest(
            operation_id=op,
            run_id=str(run),
            capability="execute_analysis",
            capability_version=1,
            side_effect=SideEffect.EXTERNAL_JOB,
        )
    )
    await world.db.tool_executions.transition(
        op, ToolExecutionStatus.SUBMITTING, attempt=1
    )
    await world.delete(alice, session, report)
    world.clock.advance(WEEK)

    held = await world.lifecycle.run_maintenance()
    assert (held.reports_purged, held.reports_blocked) == (0, 1)
    assert world.count("report_versions", "report_id = %s", report) == 1
    assert world.count("evidence_pins", "holder_id = %s", report) == 1

    await world.db.tool_executions.transition(
        op, ToolExecutionStatus.CANCELLED, attempt=1
    )
    settled = await world.lifecycle.run_maintenance()
    assert settled.reports_purged == 1 and _report_gone(world, report)


async def test_purge_racing_a_new_pin_never_leaves_a_report_without_its_evidence(
    world: World,
) -> None:
    for _ in range(6):
        alice, session = await world.executive({"1", "2", "3"})
        run = await world.run(alice, session)
        evidence_id = await world.product_evidence(alice, run)
        report_run = await world.run(alice, session)
        await world.finish(alice, session)
        world.clock.advance(WEEK + timedelta(days=1))

        async def save(owner: Principal, run_id: str, cited: str) -> str:
            saved = await world.reports.create(
                owner,
                run_id,
                draft(cited, title="Racing report"),
                operation_id=_id("op"),
            )
            return saved.version.report_id

        outcomes = await asyncio.gather(
            save(alice, report_run, evidence_id),
            world.lifecycle.run_maintenance(),
            return_exceptions=True,
        )
        saved_id, cleanup = outcomes
        assert isinstance(cleanup, MaintenanceReport)
        evidence_rows = world.count("evidence", "evidence_id = %s", evidence_id)
        if isinstance(saved_id, str):
            # The pin won: the evidence is retained for the report.
            assert evidence_rows == 1
            assert world.count("evidence_pins", "evidence_id = %s", evidence_id) == 1
            assert world.count("report_evidence", "evidence_id = %s", evidence_id) == 1
        else:
            # The cleanup won: the report was not created over missing evidence.
            assert evidence_rows == 0
            assert world.count("report_evidence", "evidence_id = %s", evidence_id) == 0
            assert isinstance(saved_id, BaseException)


# --- audit retention and independence -------------------------------------


async def test_old_audit_events_are_removed_in_bounded_batches(world: World) -> None:
    with psycopg.connect(world.stack.app_dsn) as conn:
        for index in range(5):
            conn.execute(
                "INSERT INTO audit_events (audit_id, occurred_at, actor_id, action, "
                "subject_type, subject_id, details) "
                "VALUES (%s, %s, 'a', 'x.y', 't', 's', '{}')",
                (
                    f"old-{_id('a')}-{index}",
                    world.clock.now - timedelta(days=91 + index),
                ),
            )
        conn.execute(
            "INSERT INTO audit_events (audit_id, occurred_at, actor_id, action, "
            "subject_type, subject_id, details) "
            "VALUES (%s, %s, 'a', 'x.y', 't', 's', '{}')",
            (f"new-{_id('a')}", world.clock.now - timedelta(days=89)),
        )
        conn.commit()
    service = world.make_lifecycle(audit_retention=timedelta(days=90))
    limits = MaintenanceLimits(max_audit_rows=2)
    dry = await service.run_maintenance(dry_run=True)
    assert dry.audit_rows_removed >= 5
    first = await service.run_maintenance(limits)
    assert first.audit_rows_removed == 2 and first.more_pending
    total = first.audit_rows_removed
    while True:
        step = await service.run_maintenance(limits)
        total += step.audit_rows_removed
        if not step.more_pending:
            break
    assert total >= 5
    assert (
        world.count(
            "audit_events",
            "subject_type = 't' AND occurred_at < %s",
            world.clock.now - timedelta(days=90),
        )
        == 0
    )
    assert world.count("audit_events", "audit_id LIKE 'new-%%'") == 1


async def test_ordinary_report_deletion_and_purge_leave_golden_examples_untouched(
    world: World,
) -> None:
    alice, session = await world.executive({"1", "2", "3"})
    reviewer = _id("rev")
    await world.db.access_admin.register_executive(
        ExecutiveRegistration(
            executive_id=reviewer,
            issuer="iss",
            subject=f"sub-{reviewer}",
            roles=frozenset({Role.REVIEWER}),
            label="Reviewer",
        )
    )
    await world.db.access_admin.replace_products(reviewer, {"1", "2", "3"})
    knowledge = build_knowledge(world.db, world.services, world.access.resolver)
    harness = Harness(
        service=knowledge.service,
        reader=knowledge.reader,
        index_source=knowledge.index_source,
        repository=knowledge.repository,
        artifacts=world.artifacts,
        maintenance=world.maintenance,
        ids={"author": alice.executive_id, "reviewer": reviewer},
    )
    example = await publish(harness, await submit(harness))
    report = await world.report(alice, session)
    golden_before = world.count("golden_versions")
    blobs_before = world.blob_count()

    await world.delete(alice, session, report)
    world.clock.advance(WEEK)
    await world.lifecycle.run_maintenance()

    assert _report_gone(world, report)
    assert world.count("golden_versions") == golden_before
    assert world.blob_count() == blobs_before - 1  # only the report's own artifact
    assert (await delivered(harness, example))[0] == 1


async def test_restore_denies_a_principal_that_lacks_the_permission(
    world: World,
) -> None:
    alice, session = await world.executive({"1"})
    report = await world.report(alice, session)
    await world.delete(alice, session, report)
    narrowed = Principal(
        alice.executive_id, frozenset({Permission.ANALYSIS_READ.value})
    )
    with pytest.raises(AccessDenied):
        await world.lifecycle.restore(narrowed, report)
    assert world.live_ids(alice) == set()
