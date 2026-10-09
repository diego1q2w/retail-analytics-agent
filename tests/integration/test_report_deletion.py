"""Report-deletion proposals on real PostgreSQL (Docker): locks, races, rollback."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from retail_analytics.application.authorization import AccessDenied
from retail_analytics.application.contracts.authorization import Principal
from retail_analytics.application.contracts.report_deletion import DeletionPreview
from retail_analytics.application.contracts.tools import OperationContext
from retail_analytics.application.report_deletion import ReportDeletionService
from retail_analytics.bootstrap.report_deletion import build_report_deletion
from retail_analytics.domain.report_deletion import (
    PROPOSAL_TTL,
    DeletionError,
    DeletionErrorCode,
)
from tests.integration.compose_stack import Stack, running_stack
from tests.integration.test_reports import Env, _id
from tests.unit.reports.support import draft

pytestmark = [pytest.mark.docker, pytest.mark.asyncio]


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


@pytest.fixture(scope="module")
def stack() -> Iterator[Stack]:
    with running_stack("postgres") as stack:
        stack.migrate()
        yield stack


class World(Env):
    def __init__(self, stack: Stack, root: Path) -> None:
        super().__init__(stack, root)
        self.clock = Clock()
        self.deletion: ReportDeletionService = build_report_deletion(
            self.db,
            self.access.resolver,
            clock=self.clock,
            progress=self.db.run_events,
        )

    def sql(self, statement: str, *args: object) -> list[tuple[object, ...]]:
        with psycopg.connect(self.stack.app_dsn) as conn:
            rows = conn.execute(statement, args).fetchall()
            conn.commit()
            return rows

    async def report(
        self, owner: Principal, session: str, title: str = "Revenue"
    ) -> str:
        run = await self.run(owner, session)
        evidence_id = await self.product_evidence(owner, run)
        saved = await self.reports.create(
            owner, run, draft(evidence_id, title=title), operation_id=_id("op")
        )
        return saved.version.report_id

    async def revise(self, owner: Principal, session: str, report_id: str) -> None:
        run = await self.run(owner, session)
        evidence_id = await self.product_evidence(owner, run)
        current = (await self.reports.versions(owner, report_id))[-1].version
        await self.reports.create(
            owner,
            run,
            draft(evidence_id, title="Revised"),
            operation_id=_id("op"),
            report_id=report_id,
            base_version=current,
        )

    async def propose(
        self, owner: Principal, session: str, *report_ids: str, op: str | None = None
    ) -> DeletionPreview:
        run = await self.run(owner, session)
        ctx = await self.access.resolver.context_for_run(owner, run)
        return await self.deletion.propose(
            OperationContext(ctx, op or _id("op")), tuple(report_ids)
        )

    def live_ids(self, owner: Principal) -> set[str]:
        rows = self.sql(
            "SELECT report_id FROM reports WHERE owner_id = %s AND deleted_at IS NULL",
            owner.executive_id,
        )
        return {r[0] for r in rows}  # type: ignore[misc]

    def audit_actions(self, proposal_id: str) -> list[str]:
        rows = self.sql(
            "SELECT action FROM audit_events WHERE subject_id = %s",
            proposal_id,
        )
        return sorted(str(r[0]) for r in rows)


@pytest.fixture
def world(stack: Stack, tmp_path: Path) -> Iterator[World]:
    w = World(stack, tmp_path / "artifacts")
    yield w
    w.db.close()


async def test_confirmed_deletion_is_exact_audited_and_hides_the_reports(
    world: World,
) -> None:
    alice, session = await world.executive({"1", "2", "3"})
    one = await world.report(alice, session, "Client X revenue")
    two = await world.report(alice, session, "Client X churn")
    keep = await world.report(alice, session, "Unrelated")

    preview = await world.propose(alice, session, one, two)
    assert preview.count == 2
    assert world.live_ids(alice) == {one, two, keep}
    late = await world.report(alice, session, "Created after the proposal")

    result = await world.deletion.confirm(alice, preview.proposal_id)
    assert set(result.report_ids) == {one, two}
    assert world.live_ids(alice) == {keep, late}
    assert {r.report_id for r in await world.reports.list_reports(alice)} == {
        keep,
        late,
    }
    with pytest.raises(AccessDenied):
        await world.reports.read(alice, one)
    assert (await world.reports.search(alice, "Client X")).matches == ()

    assert world.audit_actions(preview.proposal_id) == [
        "report_deletion.confirmed",
        "report_deletion.proposed",
    ]
    (details,) = world.sql(
        "SELECT details::text FROM audit_events "
        "WHERE action = 'report_deletion.confirmed' AND subject_id = %s",
        preview.proposal_id,
    )
    assert "Client X" not in str(details[0])  # identifiers and counts only
    assert one in str(details[0])


async def test_concurrent_confirmations_delete_and_audit_once(world: World) -> None:
    alice, session = await world.executive({"1", "2", "3"})
    ids = [await world.report(alice, session) for _ in range(3)]
    preview = await world.propose(alice, session, *ids)

    outcomes = await asyncio.gather(
        *(world.deletion.confirm(alice, preview.proposal_id) for _ in range(6)),
        return_exceptions=True,
    )
    wins = [o for o in outcomes if not isinstance(o, BaseException)]
    losses = [o for o in outcomes if isinstance(o, BaseException)]
    assert len(wins) == 1
    assert all(
        isinstance(e, DeletionError) and e.code is DeletionErrorCode.ALREADY_RESOLVED
        for e in losses
    )
    assert world.live_ids(alice) == set()
    assert (
        world.audit_actions(preview.proposal_id).count("report_deletion.confirmed") == 1
    )

    with pytest.raises(DeletionError):  # replay later
        await world.deletion.confirm(alice, preview.proposal_id)
    assert (
        world.audit_actions(preview.proposal_id).count("report_deletion.confirmed") == 1
    )


async def test_audit_insert_failure_rolls_back_deletion_and_consumption(
    world: World,
) -> None:
    alice, session = await world.executive({"1", "2", "3"})
    ids = [await world.report(alice, session) for _ in range(2)]
    preview = await world.propose(alice, session, *ids)

    with psycopg.connect(world.stack.app_dsn) as conn:
        conn.execute(
            "CREATE FUNCTION fail_confirm_audit() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN "
            "IF NEW.action = 'report_deletion.confirmed' THEN "
            "RAISE EXCEPTION 'injected audit failure'; END IF; RETURN NEW; END; $$"
        )
        conn.execute(
            "CREATE TRIGGER fail_confirm_audit BEFORE INSERT ON audit_events "
            "FOR EACH ROW EXECUTE FUNCTION fail_confirm_audit()"
        )
        conn.commit()
    try:
        with pytest.raises(Exception, match="injected audit failure"):
            await world.deletion.confirm(alice, preview.proposal_id)
        assert world.live_ids(alice) == set(ids)
        assert (
            await world.deletion.preview(alice, preview.proposal_id)
        ).status.value == "pending"
        assert world.audit_actions(preview.proposal_id) == ["report_deletion.proposed"]
    finally:
        with psycopg.connect(world.stack.app_dsn) as conn:
            conn.execute("DROP TRIGGER fail_confirm_audit ON audit_events")
            conn.execute("DROP FUNCTION fail_confirm_audit()")
            conn.commit()

    await world.deletion.confirm(alice, preview.proposal_id)  # still confirmable
    assert world.live_ids(alice) == set()


async def test_expiry_wrong_owner_and_stale_version_delete_nothing(
    world: World,
) -> None:
    alice, session = await world.executive({"1", "2", "3"})
    bob, bob_session = await world.executive({"2"})
    one = await world.report(alice, session)
    two = await world.report(alice, session)
    theirs = await world.report(bob, bob_session)

    with pytest.raises(AccessDenied):  # not bob's report
        await world.propose(alice, session, one, theirs)

    expiring = await world.propose(alice, session, one)
    with pytest.raises(AccessDenied):
        await world.deletion.confirm(bob, expiring.proposal_id)
    world.clock.advance(PROPOSAL_TTL)
    with pytest.raises(DeletionError) as expired:
        await world.deletion.confirm(alice, expiring.proposal_id)
    assert expired.value.code is DeletionErrorCode.EXPIRED

    stale = await world.propose(alice, session, one, two)
    await world.revise(alice, session, two)
    with pytest.raises(DeletionError) as changed:
        await world.deletion.confirm(alice, stale.proposal_id)
    assert changed.value.code is DeletionErrorCode.STALE
    assert world.live_ids(alice) == {one, two}  # all or nothing
    assert world.live_ids(bob) == {theirs}
    assert world.audit_actions(stale.proposal_id) == ["report_deletion.proposed"]


async def test_cancel_and_no_confirmation_leave_reports_alone(world: World) -> None:
    alice, session = await world.executive({"1", "2", "3"})
    one = await world.report(alice, session)
    preview = await world.propose(alice, session, one)
    assert world.live_ids(alice) == {one}
    await world.deletion.cancel(alice, preview.proposal_id)
    with pytest.raises(DeletionError) as error:
        await world.deletion.confirm(alice, preview.proposal_id)
    assert error.value.code is DeletionErrorCode.ALREADY_RESOLVED
    assert world.live_ids(alice) == {one}
    assert world.audit_actions(preview.proposal_id) == [
        "report_deletion.cancelled",
        "report_deletion.proposed",
    ]


async def test_confirmation_racing_a_new_version_never_deletes_unconfirmed_content(
    world: World,
) -> None:
    alice, session = await world.executive({"1", "2", "3"})
    one = await world.report(alice, session)
    preview = await world.propose(alice, session, one)
    results = await asyncio.gather(
        world.deletion.confirm(alice, preview.proposal_id),
        world.revise(alice, session, one),
        return_exceptions=True,
    )
    versions = world.sql(
        "SELECT count(*) FROM report_versions WHERE report_id = %s", one
    )[0][0]
    if world.live_ids(alice) == set():
        # Deleted: it was confirmed against version 1 and no later version exists.
        assert not isinstance(results[0], BaseException)
        assert versions == 1
    else:
        assert isinstance(results[0], DeletionError)
        assert results[0].code is DeletionErrorCode.STALE
        assert versions == 2


async def test_proposal_is_retry_safe_and_single_use_in_the_database(
    world: World,
) -> None:
    alice, session = await world.executive({"1", "2", "3"})
    one = await world.report(alice, session)
    op = _id("op")
    first = await world.propose(alice, session, one, op=op)
    again = await world.propose(alice, session, one, op=op)
    assert again.proposal_id == first.proposal_id and again.duplicate

    await world.deletion.confirm(alice, first.proposal_id)
    with psycopg.connect(world.stack.app_dsn) as conn:
        with pytest.raises(psycopg.errors.RestrictViolation):
            conn.execute(
                "UPDATE deletion_proposals SET status = 'pending', resolved_at = NULL "
                "WHERE proposal_id = %s",
                (first.proposal_id,),
            )
        conn.rollback()
        with pytest.raises(psycopg.errors.RestrictViolation):
            conn.execute("UPDATE audit_events SET actor_id = 'x'")
        conn.rollback()
