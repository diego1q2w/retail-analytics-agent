"""Soft-deleting a report stops reuse through it (T18-F5, Docker).

Real PostgreSQL: the deletion confirmation withdraws, in its own transaction,
every link the report gave the owner's other sessions; the figures reached
only through it leave context, citations, fetches and reuse (and so does
evidence derived from them), while evidence obtained in the session itself or
still linked through another valid report stays usable. Restoring re-validates
each link (access, definitions, evidence validity) before any reuse and
records the outcome. Purge is unchanged.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from retail_analytics.adapters.postgres.database import Database
from retail_analytics.adapters.postgres.product_scopes import (
    PostgresProductScopeSnapshots,
)
from retail_analytics.adapters.postgres.reports import PostgresReportRepository
from retail_analytics.application.contracts.authorization import Principal
from retail_analytics.application.contracts.persistence import OperationRequest
from retail_analytics.application.contracts.tools import OperationContext
from retail_analytics.application.evidence import ReuseRequest
from retail_analytics.application.lifecycle import LifecycleService
from retail_analytics.application.output_privacy import (
    OutputDestination,
    OutputSection,
    OutputWithheld,
)
from retail_analytics.application.reports import ReportService
from retail_analytics.application.result_privacy import PRIVACY_POLICY_VERSION
from retail_analytics.bootstrap.lifecycle import build_lifecycle
from retail_analytics.domain.conversation import MessageRole
from retail_analytics.domain.evidence import (
    Evidence,
    EvidenceContent,
    EvidenceKind,
    EvidenceTable,
    Provenance,
    Requirements,
    ReuseBlock,
    ReuseIntent,
)
from retail_analytics.domain.lifecycle import RestoreError, RestoreErrorCode
from retail_analytics.domain.logical_catalog import default_logical_catalog
from retail_analytics.domain.metrics import default_catalog
from retail_analytics.domain.operations import SideEffect
from retail_analytics.domain.report_deletion import RECOVERY_PERIOD
from tests.integration.compose_stack import Stack, running_stack
from tests.integration.test_lifecycle import World as LifecycleWorld
from tests.integration.test_report_reuse import FINGERPRINT, SEPTEMBER
from tests.integration.test_report_reuse import Env as ReuseEnv
from tests.integration.test_reports import _id
from tests.unit.evidence.support import REVENUE
from tests.unit.reports.support import PRODUCT_COLUMNS, draft

pytestmark = [pytest.mark.docker, pytest.mark.asyncio]

ANSWER = "Coat earned 48213.75 in September."


@pytest.fixture(scope="module")
def stack() -> Iterator[Stack]:
    with running_stack("postgres") as stack:
        stack.migrate()
        yield stack


class World(LifecycleWorld, ReuseEnv):
    """Deletion, restore and purge (fake clock) plus cross-session reuse."""

    def __init__(self, stack: Stack, root: Path) -> None:
        super().__init__(stack, root)
        self.restorer = self.with_reuse(self.reports)

    def with_reuse(self, reuse: ReportService | None) -> LifecycleService:
        return build_lifecycle(
            self.db, self.access.resolver, self.services, clock=self.clock, reuse=reuse
        )

    async def report_on(
        self, owner: Principal, session: str, evidence_id: str, title: str
    ) -> str:
        run = await self.run(owner, session)
        saved = await self.reports.create(
            owner, run, draft(evidence_id, title=title), operation_id=_id("op")
        )
        return saved.version.report_id

    async def derived(self, owner: Principal, run: str, source: str) -> str:
        ctx = await self.access.resolver.context_for_run(owner, run)
        op = await self.db.tool_executions.begin(
            OperationRequest(
                operation_id=_id("op"),
                run_id=run,
                capability="convert_currency",
                capability_version=1,
                side_effect=SideEffect.READ_ONLY,
            )
        )
        record = await self.evidence.record(
            OperationContext(ctx, op.execution.operation_id),
            EvidenceContent(
                kind=EvidenceKind.DERIVED,
                subject_key="d:september-revenue-share",
                analysis=(await self.evidence_record(source)).content.analysis,
                provenance=Provenance(notes=(("derived", "share"),)),
                table=EvidenceTable(
                    columns=PRODUCT_COLUMNS,
                    rows=((101, "Coat", "Acme", 0.6),),
                    received_rows=1,
                ),
                grain=("product_id",),
                derived_from=(source,),
            ),
        )
        return record.evidence_id

    async def evidence_record(self, evidence_id: str) -> Evidence:
        stored = await self.db.evidence.get(evidence_id)
        assert stored is not None
        return stored.evidence

    async def answer(
        self, owner: Principal, session: str, run: str, evidence_id: str
    ) -> None:
        text = await self.cite(owner, run, evidence_id)
        await self.db.sessions.append_message(
            message_id=_id("msg"),
            session_id=session,
            role=MessageRole.ASSISTANT,
            content=text,
            run_id=run,
        )

    async def cite_text(
        self, owner: Principal, run: str, evidence_id: str, text: str
    ) -> str:
        (released,) = await self.context.gate.check(
            owner,
            run,
            [OutputSection("answer", text, (evidence_id,))],
            OutputDestination.DISPLAY,
        )
        return released.text

    def links(self, session: str) -> dict[str, tuple[object, ...]]:
        rows = self.sql(
            "SELECT report_id, withdrawn_reason, revalidation_result, "
            "withdrawn_at IS NOT NULL, revalidated_at IS NOT NULL "
            "FROM session_report_evidence WHERE session_id = %s",
            session,
        )
        return {str(r[0]): tuple(r[1:]) for r in rows}

    def audit_details(self, action: str, subject_id: str) -> list[dict[str, object]]:
        rows = self.sql(
            "SELECT details::text FROM audit_events WHERE action = %s "
            "AND subject_id = %s ORDER BY occurred_at",
            action,
            subject_id,
        )
        return [json.loads(str(r[0])) for r in rows]

    def audit_by_action(self, action: str) -> list[dict[str, object]]:
        rows = self.sql(
            "SELECT details::text FROM audit_events WHERE action = %s", action
        )
        return [json.loads(str(r[0])) for r in rows]

    async def report_session(self, report_id: str) -> str:
        """The session the report was saved from (where its evidence lives)."""
        ((session,),) = self.sql(
            "SELECT r.session_id FROM report_versions v JOIN runs r "
            "ON r.run_id = v.run_id WHERE v.report_id = %s AND v.version = 1",
            report_id,
        )
        return str(session)

    async def explain(self, owner: Principal, run: str) -> object:
        ctx = await self.access.resolver.context_for_run(owner, run)
        return await self.evidence.find_reusable(
            ctx,
            ReuseRequest(
                ReuseIntent.EXPLAIN,
                Requirements(
                    catalog_version=default_logical_catalog().version,
                    policy_version=PRIVACY_POLICY_VERSION,
                    preference_fingerprint=FINGERPRINT,
                    definitions=frozenset({REVENUE}),
                    period=SEPTEMBER,
                ),
                subject_key="q:september-revenue-by-product",
            ),
        )


@pytest.fixture
def world(stack: Stack, tmp_path: Path) -> Iterator[World]:
    w = World(stack, tmp_path / "artifacts")
    yield w
    w.db.close()


async def test_deletion_withdraws_links_and_excludes_dependent_answers(
    world: World,
) -> None:
    owner, report_id, evidence_id = await world.saved_report({"1", "2", "3"})
    session, run = await world.new_session_run(owner)
    assert (await world.reuse(owner, run, report_id)).linked == (  # type: ignore[attr-defined]
        evidence_id,
    )
    await world.answer(owner, session, run, evidence_id)
    derived = await world.derived(owner, run, evidence_id)
    assert "0.6" in await world.cite_text(owner, run, derived, "Coat share 0.6.")
    owner_session = await world.report_session(report_id)
    await world.delete(owner, owner_session, report_id)

    # Withdrawn in the deletion transaction, and audited with it.
    assert world.links(session) == {report_id: ("report_deleted", None, True, False)}
    (confirmed,) = [
        d
        for d in world.audit_by_action("report_deletion.confirmed")
        if report_id in json.dumps(d)
    ]
    assert confirmed["reuse_links_withdrawn"] == 1

    # Not citable, and its derivative neither.
    with pytest.raises(OutputWithheld) as withheld:
        await world.cite(owner, run, evidence_id)
    assert withheld.value.reason == "unavailable_evidence"
    with pytest.raises(OutputWithheld):
        await world.cite_text(owner, run, derived, "Coat share 0.6.")

    # Future model context: no evidence, the dependent answer is withheld.
    later = await world.run(owner, session)
    built = await world.context.builder.build(owner, later, "And the coat?")
    assert built.evidence == ()
    assert built.omissions.history_access_changed == 1
    assert "48213.75" not in built.render()
    # fetch_evidence and find_reusable stop offering it.
    assert await world.context.builder.read_evidence(owner, later, evidence_id) is None
    assert await world.context.builder.read_evidence(owner, later, derived) is None
    listed = await world.context.builder.list_evidence(owner, later)
    assert {x.evidence_id for x in listed} == set()
    outcome = await world.explain(owner, later)
    assert outcome.reused is None  # type: ignore[attr-defined]
    assert (evidence_id, ReuseBlock.REPORT_LINK_WITHDRAWN) in outcome.considered  # type: ignore[attr-defined]


async def test_a_late_import_after_deletion_links_nothing(world: World) -> None:
    """A report read before the deletion cannot be imported after it."""
    owner, report_id, evidence_id = await world.saved_report({"1", "2", "3"})
    session, run = await world.new_session_run(owner)
    document = await world.reports.read(owner, report_id)
    await world.delete(owner, await world.report_session(report_id), report_id)

    outcome = await world.reports.reuse_in_run(
        owner, run, document, preference_fingerprint=FINGERPRINT
    )
    assert outcome.linked == ()
    assert outcome.refused == ((evidence_id, ReuseBlock.REPORT_LINK_WITHDRAWN),)
    assert world.links(session) == {}
    with pytest.raises(OutputWithheld):
        await world.cite(owner, run, evidence_id)


async def test_independently_obtained_evidence_stays_usable(world: World) -> None:
    owner, report_id, evidence_id = await world.saved_report({"1", "2", "3"})
    origin = await world.report_session(report_id)
    session, run = await world.new_session_run(owner)
    await world.reuse(owner, run, report_id)
    await world.answer(owner, session, run, evidence_id)

    # This session's own analysis, after the import.
    own_run = await world.run(owner, session)
    own = await world.evidence_at(owner, own_run, datetime.now(UTC))
    await world.answer(owner, session, own_run, own)

    await world.delete(owner, origin, report_id)

    later = await world.run(owner, session)
    assert "48213.75" in await world.cite(owner, later, own)
    built = await world.context.builder.build(owner, later, "Compare them")
    assert [e.evidence_id for e in built.evidence] == [own]
    assert built.omissions.history_access_changed == 1  # only the report answer
    assert built.render().count("48213.75") >= 1  # the own answer and evidence
    # The session that computed the report's evidence keeps using it.
    origin_run = await world.run(owner, origin)
    assert "48213.75" in await world.cite(owner, origin_run, evidence_id)


async def test_evidence_shared_with_another_valid_report_stays_usable(
    world: World,
) -> None:
    owner, first, evidence_id = await world.saved_report({"1", "2", "3"})
    origin = await world.report_session(first)
    second = await world.report_on(owner, origin, evidence_id, "Second view")
    session, run = await world.new_session_run(owner)
    await world.reuse(owner, run, first)
    await world.reuse(owner, run, second)
    await world.answer(owner, session, run, evidence_id)

    await world.delete(owner, origin, first)

    links = world.links(session)
    assert links[first][0] == "report_deleted"
    assert links[second] == (None, None, False, False)
    later = await world.run(owner, session)
    assert "48213.75" in await world.cite(owner, later, evidence_id)
    built = await world.context.builder.build(owner, later, "Again?")
    assert [e.evidence_id for e in built.evidence] == [evidence_id]
    assert built.omissions.history_access_changed == 0
    assert 'from saved report "Second view"' in built.render()

    # Once the other report goes too, nothing reaches it.
    await world.delete(owner, origin, second)
    with pytest.raises(OutputWithheld):
        await world.cite(owner, later, evidence_id)


async def test_restore_revalidates_before_any_reuse(world: World) -> None:
    owner, report_id, evidence_id = await world.saved_report({"1", "2", "3"})
    origin = await world.report_session(report_id)
    session, run = await world.new_session_run(owner)
    await world.reuse(owner, run, report_id)
    await world.answer(owner, session, run, evidence_id)
    await world.delete(owner, origin, report_id)

    # Restoring alone never revives a link.
    unwired = world.with_reuse(None)
    restored = await unwired.restore(owner, report_id)
    assert restored.reuse_links_pending == 1 and restored.reuse is None
    assert world.links(session) == {
        report_id: ("revalidation_pending", None, True, False)
    }
    with pytest.raises(OutputWithheld):
        await world.cite(owner, run, evidence_id)
    (details,) = world.audit_details("report.restored", report_id)
    assert details["reuse_links_pending_revalidation"] == 1

    # The re-validation checks it again and records the outcome.
    outcome = await world.reports.revalidate_restored(
        report_id, owner_id=owner.executive_id, actor_id=owner.executive_id
    )
    assert outcome.reinstated == 1 and outcome.refused == {}
    assert world.links(session) == {report_id: (None, "reinstated", False, True)}
    (audit,) = world.audit_details("report.reuse_revalidated", report_id)
    assert audit["links_reinstated"] == 1 and audit["links_refused"] == {}
    later = await world.run(owner, session)
    assert "48213.75" in await world.cite(owner, later, evidence_id)
    built = await world.context.builder.build(owner, later, "Back?")
    assert built.omissions.history_access_changed == 0

    # The wired restore does both steps.
    await world.delete(owner, origin, report_id)
    again = await world.restorer.restore(owner, report_id)
    assert again.reuse is not None and again.reuse.reinstated == 1
    assert "48213.75" in await world.cite(owner, later, evidence_id)


async def test_restore_keeps_links_withdrawn_when_access_changed(world: World) -> None:
    owner, report_id, evidence_id = await world.saved_report({"1", "2", "3"})
    origin = await world.report_session(report_id)
    session, run = await world.new_session_run(owner)
    await world.reuse(owner, run, report_id)
    await world.delete(owner, origin, report_id)
    await world.db.access_admin.replace_products(owner.executive_id, {"1", "3"})

    restored = await world.restorer.restore(owner, report_id)
    assert restored.reuse is not None
    assert restored.reuse.reinstated == 0
    assert restored.reuse.refused == {"authorization_changed": 1}
    assert world.links(session) == {
        report_id: ("revalidation_failed", "authorization_changed", True, True)
    }
    # Even with the products back, the failed link stays withdrawn until the
    # report is read (and checked) again in that session.
    await world.db.access_admin.replace_products(owner.executive_id, {"1", "2", "3"})
    later = await world.run(owner, session)
    with pytest.raises(OutputWithheld):
        await world.cite(owner, later, evidence_id)
    assert (await world.reuse(owner, later, report_id)).linked == (  # type: ignore[attr-defined]
        evidence_id,
    )
    assert world.links(session) == {report_id: (None, "reimported", False, True)}
    assert "48213.75" in await world.cite(owner, later, evidence_id)


async def test_restore_keeps_links_withdrawn_when_definitions_changed(
    world: World,
) -> None:
    owner, report_id, evidence_id = await world.saved_report({"1", "2", "3"})
    origin = await world.report_session(report_id)
    session, run = await world.new_session_run(owner)
    await world.reuse(owner, run, report_id)

    # A newer catalog: the record's meaning is no longer the current one.
    db = Database(world.db.engine)
    newer = ReportService(
        PostgresReportRepository(db),
        world.artifacts,
        world.evidence,
        world.context.gate,
        world.access.resolver,
        default_catalog(),
        PostgresProductScopeSnapshots(db),
        world.db.preferences,
        catalog_version=default_logical_catalog().version + 1,
    )
    await world.delete(owner, origin, report_id)
    restored = await world.with_reuse(newer).restore(owner, report_id)
    assert restored.reuse is not None
    assert restored.reuse.refused == {"catalog_changed": 1}
    assert world.links(session)[report_id][:2] == (
        "revalidation_failed",
        "catalog_changed",
    )
    with pytest.raises(OutputWithheld):
        await world.cite(owner, run, evidence_id)

    # A persistent definition change (invalidated finding) is refused too.
    await world.delete(owner, origin, report_id)
    await world.db.evidence.invalidate_dependent_findings(
        owner.executive_id, None, "metric_definition:revenue"
    )
    again = await world.restorer.restore(owner, report_id)
    assert again.reuse is not None and again.reuse.refused == {"invalidated": 1}


async def test_unknown_definitions_are_never_reinstated(world: World) -> None:
    """A link to a record without recorded definitions (compatibility
    unknown) is refused by the re-validation, never treated as compatible."""
    owner, report_id, evidence_id = await world.saved_report(
        {"1", "2", "3"}, recorded=False
    )
    session, run = await world.new_session_run(owner)
    # Such a link cannot be created through an import; plant one directly.
    world.execute(
        "INSERT INTO session_report_evidence (session_id, evidence_id, "
        "executive_id, run_id, report_id, report_version, report_title, "
        "imported_at) VALUES (%s, %s, %s, %s, %s, 1, 't', now())",
        session,
        evidence_id,
        owner.executive_id,
        run,
        report_id,
    )
    await world.delete(owner, await world.report_session(report_id), report_id)
    restored = await world.restorer.restore(owner, report_id)
    assert restored.reuse is not None
    assert restored.reuse.refused == {"definitions_unknown": 1}
    with pytest.raises(OutputWithheld):
        await world.cite(owner, run, evidence_id)


async def test_purge_path_is_unchanged(world: World) -> None:
    owner, report_id, evidence_id = await world.saved_report({"1", "2", "3"})
    origin = await world.report_session(report_id)
    session, run = await world.new_session_run(owner)
    await world.reuse(owner, run, report_id)
    await world.delete(owner, origin, report_id)

    world.clock.advance(RECOVERY_PERIOD)
    result = await world.restorer.run_maintenance()
    # Other tests' deleted reports share this database and become due too.
    assert result.reports_purged >= 1 and result.reports_failed == 0
    assert world.count("reports", "report_id = %s", report_id) == 0
    assert world.audit_actions_for(report_id)[-1] == "report.purged"
    with pytest.raises(RestoreError) as gone:
        await world.restorer.restore(owner, report_id)
    assert gone.value.code is RestoreErrorCode.NOT_FOUND
    # The withdrawn link never comes back.
    assert world.links(session)[report_id][0] == "report_deleted"
    with pytest.raises(OutputWithheld):
        await world.cite(owner, run, evidence_id)
    again = await world.restorer.run_maintenance()  # idempotent
    assert again.reports_purged == 0 and again.reports_failed == 0
