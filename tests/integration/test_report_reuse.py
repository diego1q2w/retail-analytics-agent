"""Reusing a saved report's evidence in the owner's new session (T18-F2, Docker).

Real PostgreSQL: the same executive opens a new session, reads their report
and may cite its figures (with source and date) after the automatic checks
only; losing a required product refuses reuse and withholds what was already
cited; widened access keeps working; nobody else can reuse it; changed
definitions/settings are not reused; current-data questions query again.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from retail_analytics.application.authorization import AccessDenied
from retail_analytics.application.contracts.authorization import Principal
from retail_analytics.application.contracts.persistence import OperationRequest
from retail_analytics.application.contracts.tools import OperationContext
from retail_analytics.application.evidence import ReuseRequest
from retail_analytics.application.output_privacy import (
    OutputDestination,
    OutputSection,
    OutputWithheld,
)
from retail_analytics.application.result_privacy import PRIVACY_POLICY_VERSION
from retail_analytics.bootstrap.context import build_context
from retail_analytics.bootstrap.preferences import build_preferences
from retail_analytics.domain.conversation import MessageRole
from retail_analytics.domain.evidence import (
    AnalysisCompatibility,
    AnalysisStamp,
    EvidenceContent,
    EvidenceKind,
    EvidenceTable,
    Provenance,
    Requirements,
    ReuseBlock,
    ReuseIntent,
    TermMeaning,
)
from retail_analytics.domain.logical_catalog import default_logical_catalog
from retail_analytics.domain.operations import SideEffect
from retail_analytics.domain.periods import DateWindow
from retail_analytics.domain.preferences import EffectivePreferences
from retail_analytics.domain.report_definitions import DefinitionNoticeKind
from retail_analytics.domain.reports import ReportError, ReportErrorCode
from tests.integration.compose_stack import Stack, running_stack
from tests.integration.test_reports import Env as ReportsEnv
from tests.unit.evidence.support import REVENUE
from tests.unit.reports.support import PRODUCT_COLUMNS, draft

pytestmark = [pytest.mark.docker, pytest.mark.asyncio]

SEPTEMBER = DateWindow(date(2026, 9, 1), date(2026, 10, 1))
ROWS: tuple[tuple[object, ...], ...] = (
    (101, "Coat", "Acme", 48213.75),
    (102, "Scarf", "Zed", 31877.4),
)
SLOT = "metric_definition:revenue"
# The analytical fingerprint of an executive without preferences (real value,
# so the model-facing tool path, which reads effective preferences, agrees).
FINGERPRINT = EffectivePreferences(()).analytical_fingerprint


@pytest.fixture(scope="module")
def stack() -> Iterator[Stack]:
    with running_stack("postgres") as stack:
        stack.migrate()
        yield stack


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class Env(ReportsEnv):
    def __init__(self, stack: Stack, root: Path) -> None:
        super().__init__(stack, root)
        self.preferences = build_preferences(self.db, self.access)
        self.context = build_context(
            self.db, self.access, self.evidence, self.preferences
        )

    async def evidence_at(
        self,
        principal: Principal,
        run_id: str,
        computed_at: datetime,
        *,
        recorded: bool = True,
    ) -> str:
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
            subject_key="q:september-revenue-by-product",
            analysis=AnalysisStamp(
                default_logical_catalog().version,
                PRIVACY_POLICY_VERSION,
                frozenset({REVENUE}),
                FINGERPRINT,
                period=SEPTEMBER,
                terms=(
                    frozenset({TermMeaning("revenue", REVENUE)})
                    if recorded
                    else frozenset()
                ),
                date_basis="ordered_date" if recorded else None,
                definitions_recorded=recorded,
            ),
            provenance=Provenance(notes=(("source", "fixture"),)),
            table=EvidenceTable(
                columns=PRODUCT_COLUMNS,
                rows=ROWS,  # type: ignore[arg-type]
                received_rows=len(ROWS),
            ),
            grain=("product_id",),
            analytical_slots=frozenset({SLOT}),
        )
        record = await self.evidence.record(
            OperationContext(ctx, op.execution.operation_id),
            content,
            computed_at=computed_at,
        )
        return record.evidence_id

    async def saved_report(
        self, products: set[str], *, recorded: bool = True
    ) -> tuple[Principal, str, str]:
        """An executive with a report saved two days ago in another session."""
        owner, session = await self.executive(products)
        run = await self.run(owner, session)
        computed = datetime.now(UTC) - timedelta(days=2)
        evidence_id = await self.evidence_at(owner, run, computed, recorded=recorded)
        saved = await self.reports.create(
            owner, run, draft(evidence_id), operation_id=_id("op")
        )
        return owner, saved.version.report_id, evidence_id

    async def new_session_run(self, owner: Principal) -> tuple[str, str]:
        session = await self.db.sessions.create_session(_id("ses"), owner.executive_id)
        return session.session_id, await self.run(owner, session.session_id)

    async def reuse(self, owner: Principal, run: str, report_id: str) -> object:
        document = await self.reports.read(owner, report_id)
        return await self.reports.reuse_in_run(
            owner, run, document, preference_fingerprint=FINGERPRINT
        )

    async def cite(self, owner: Principal, run: str, evidence_id: str) -> str:
        (released,) = await self.context.gate.check(
            owner,
            run,
            [
                OutputSection(
                    "answer", "Coat earned 48213.75 in September.", (evidence_id,)
                )
            ],
            OutputDestination.DISPLAY,
        )
        return released.text


@pytest.fixture
def env(stack: Stack, tmp_path: Path) -> Iterator[Env]:
    e = Env(stack, tmp_path / "artifacts")
    yield e
    e.db.close()


async def test_same_user_new_session_cites_with_source_and_date(env: Env) -> None:
    owner, report_id, evidence_id = await env.saved_report({"1", "2", "3"})
    session, run = await env.new_session_run(owner)

    # Before reading the report, its evidence is not this session's evidence.
    with pytest.raises(OutputWithheld):
        await env.cite(owner, run, evidence_id)

    outcome = await env.reuse(owner, run, report_id)
    assert outcome.linked == (evidence_id,)  # type: ignore[attr-defined]
    assert "Coat earned 48213.75" in await env.cite(owner, run, evidence_id)

    built = await env.context.builder.build(owner, run, "What did my report say?")
    rendered = built.render()
    assert 'source: from saved report "September revenue by product" (v1)' in rendered
    assert "historical snapshot, not current data" in rendered
    assert "; period " in rendered
    page = await env.context.builder.read_evidence(owner, run, evidence_id)
    assert page is not None and page.source is not None
    assert (
        "computed " + (datetime.now(UTC) - timedelta(days=2)).strftime("%Y-%m-%d")
        in page.source
    )
    # The link is provenance for this run's messages (HistoryRules).
    links = await env.db.evidence.for_run(run)
    assert evidence_id in {x.evidence_id for x in links}
    # A later run of the same session keeps it citable.
    later = await env.run(owner, session)
    assert "48213.75" in await env.cite(owner, later, evidence_id)


async def test_unrecorded_definitions_read_with_notice_but_are_recomputed(
    env: Env,
) -> None:
    """A report whose evidence predates recorded definitions stays readable
    with a neutral notice; its figures are never reused (compatibility
    unknown, not compatible)."""
    owner, report_id, evidence_id = await env.saved_report(
        {"1", "2", "3"}, recorded=False
    )
    _, run = await env.new_session_run(owner)

    document = await env.reports.read(owner, report_id)
    (notice,) = document.notices
    assert notice.kind is DefinitionNoticeKind.DEFINITIONS_NOT_RECORDED
    assert "were not recorded" in notice.message
    outcome = await env.reuse(owner, run, report_id)
    assert outcome.linked == ()  # type: ignore[attr-defined]
    assert outcome.refused == (  # type: ignore[attr-defined]
        (evidence_id, ReuseBlock.DEFINITIONS_UNKNOWN),
    )
    with pytest.raises(OutputWithheld):
        await env.cite(owner, run, evidence_id)
    # A report with recorded, unchanged definitions shows no notice.
    owner2, current, _ = await env.saved_report({"1", "2", "3"})
    assert (await env.reports.read(owner2, current)).notices == ()


async def test_narrowed_scope_refuses_and_withholds_existing_citations(
    env: Env,
) -> None:
    owner, report_id, evidence_id = await env.saved_report({"1", "2", "3"})
    session, run = await env.new_session_run(owner)
    await env.reuse(owner, run, report_id)
    answer = await env.cite(owner, run, evidence_id)
    await env.db.sessions.append_message(
        message_id=_id("msg"),
        session_id=session,
        role=MessageRole.ASSISTANT,
        content=answer,
        run_id=run,
    )

    await env.db.access_admin.replace_products(owner.executive_id, {"1", "3"})

    with pytest.raises(ReportError) as refused:
        await env.reuse(owner, run, report_id)
    assert refused.value.code is ReportErrorCode.ACCESS_CHANGED
    ctx = await env.access.resolver.context_for_run(owner, run)
    direct = await env.evidence.import_report_evidence(
        ctx,
        report_id=report_id,
        report_version=1,
        report_title="t",
        evidence_ids=[evidence_id],
        compatibility=_compatibility(),
    )
    assert direct.refused == ((evidence_id, ReuseBlock.AUTHORIZATION_CHANGED),)

    # Existing citations: no longer citable, figure blocked, answer withheld.
    with pytest.raises(OutputWithheld) as withheld:
        await env.cite(owner, run, evidence_id)
    assert withheld.value.reason == "unavailable_evidence"
    later = await env.run(owner, session)
    with pytest.raises(OutputWithheld) as figure:
        await env.context.gate.check(
            owner,
            later,
            [OutputSection("answer", "Coat earned 48213.75.")],
            OutputDestination.DISPLAY,
        )
    assert figure.value.reason == "out_of_scope_figure"
    built = await env.context.builder.build(owner, later, "And now?")
    assert built.evidence == ()
    assert built.omissions.history_access_changed == 1
    assert "48213.75" not in built.render()
    assert await env.context.builder.read_evidence(owner, later, evidence_id) is None


async def test_widened_scope_still_allows_reuse(env: Env) -> None:
    owner, report_id, evidence_id = await env.saved_report({"1", "2"})
    await env.db.access_admin.replace_products(owner.executive_id, {"1", "2", "3"})
    _, run = await env.new_session_run(owner)
    outcome = await env.reuse(owner, run, report_id)
    assert outcome.linked == (evidence_id,)  # type: ignore[attr-defined]
    assert "48213.75" in await env.cite(owner, run, evidence_id)


async def test_another_user_cannot_find_or_reuse(env: Env) -> None:
    _owner, report_id, evidence_id = await env.saved_report({"1", "2", "3"})
    other, session = await env.executive({"1", "2", "3"})
    run = await env.run(other, session)
    with pytest.raises(AccessDenied):
        await env.reports.read(other, report_id)
    ctx = await env.access.resolver.context_for_run(other, run)
    outcome = await env.evidence.import_report_evidence(
        ctx,
        report_id=report_id,
        report_version=1,
        report_title="t",
        evidence_ids=[evidence_id],
        compatibility=_compatibility(),
    )
    assert outcome.linked == ()
    assert outcome.refused == ((evidence_id, ReuseBlock.NOT_OWNED),)
    with pytest.raises(OutputWithheld):
        await env.cite(other, run, evidence_id)
    # The store refuses a forged import too.
    with pytest.raises(AccessDenied):
        from retail_analytics.application.contracts.evidence import (
            NewEvidenceImport,
        )

        await env.db.evidence.add_import(
            NewEvidenceImport(
                session, evidence_id, other.executive_id, run, report_id, 1, "t"
            )
        )


async def test_changed_definition_or_settings_are_not_reused(env: Env) -> None:
    owner, report_id, evidence_id = await env.saved_report({"1", "2", "3"})
    _, run = await env.new_session_run(owner)
    document = await env.reports.read(owner, report_id)

    other_settings = await env.reports.reuse_in_run(
        owner, run, document, preference_fingerprint="fp-changed"
    )
    assert other_settings.refused == ((evidence_id, ReuseBlock.PREFERENCES_CHANGED),)
    ctx = await env.access.resolver.context_for_run(owner, run)
    newer = await env.evidence.import_report_evidence(
        ctx,
        report_id=report_id,
        report_version=1,
        report_title="t",
        evidence_ids=[evidence_id],
        compatibility=_compatibility(current_definitions={REVENUE.metric_id: 2}),
    )
    assert newer.refused == ((evidence_id, ReuseBlock.DEFINITIONS_CHANGED),)

    # A persistent definition change invalidates the finding: not reused,
    # but the historical report itself stays readable.
    await env.db.evidence.invalidate_dependent_findings(owner.executive_id, None, SLOT)
    invalidated = await env.reuse(owner, run, report_id)
    assert invalidated.refused == (  # type: ignore[attr-defined]
        (evidence_id, ReuseBlock.INVALIDATED),
    )
    assert (await env.reports.read(owner, report_id)).markdown
    with pytest.raises(OutputWithheld):
        await env.cite(owner, run, evidence_id)


async def test_session_setting_change_supersedes_an_import(env: Env) -> None:
    owner, report_id, evidence_id = await env.saved_report({"1", "2", "3"})
    session, run = await env.new_session_run(owner)
    await env.reuse(owner, run, report_id)
    await env.db.evidence.invalidate_dependent_findings(
        owner.executive_id, session, SLOT
    )
    with pytest.raises(OutputWithheld):
        await env.cite(owner, run, evidence_id)
    # Only this session's link is superseded; the report still reuses elsewhere.
    _, other_run = await env.new_session_run(owner)
    assert (await env.reuse(owner, other_run, report_id)).linked == (  # type: ignore[attr-defined]
        evidence_id,
    )


async def test_current_questions_requery_instead_of_reusing(env: Env) -> None:
    owner, report_id, evidence_id = await env.saved_report({"1", "2", "3"})
    _, run = await env.new_session_run(owner)
    await env.reuse(owner, run, report_id)
    ctx = await env.access.resolver.context_for_run(owner, run)

    def request(intent: ReuseIntent) -> ReuseRequest:
        return ReuseRequest(
            intent,
            Requirements(
                catalog_version=default_logical_catalog().version,
                policy_version=PRIVACY_POLICY_VERSION,
                preference_fingerprint=FINGERPRINT,
                definitions=frozenset({REVENUE}),
                period=SEPTEMBER,
            ),
            subject_key="q:september-revenue-by-product",
        )

    current = await env.evidence.find_reusable(ctx, request(ReuseIntent.CURRENT))
    assert current.reused is None
    assert current.considered == ((evidence_id, ReuseBlock.STALE),)
    assert current.refresh_of is None
    explained = await env.evidence.find_reusable(ctx, request(ReuseIntent.EXPLAIN))
    assert explained.reused is not None
    assert explained.reused.evidence.evidence_id == evidence_id


def _compatibility(**overrides: object) -> AnalysisCompatibility:
    values: dict[str, object] = {
        "catalog_version": default_logical_catalog().version,
        "policy_version": PRIVACY_POLICY_VERSION,
        "preference_fingerprint": FINGERPRINT,
        "current_definitions": {REVENUE.metric_id: REVENUE.version},
    }
    values.update(overrides)
    return AnalysisCompatibility(**values)  # type: ignore[arg-type]


class _Principals:
    def __init__(self, principal: Principal) -> None:
        self._principal = principal

    async def get(self, run_id: str) -> Principal | None:
        return self._principal


async def test_read_report_tool_links_reusable_evidence(env: Env) -> None:
    """The model-facing path: reading the report is all it takes."""
    from retail_analytics.application.tools import ToolSucceeded
    from retail_analytics.capabilities.reports import (
        READ_REPORT,
        ReadReportInput,
        report_capabilities,
    )

    owner, report_id, evidence_id = await env.saved_report({"1", "2", "3"})
    _, run = await env.new_session_run(owner)
    specs = {
        s.name: s
        for s in report_capabilities(
            env.reports,
            principals=_Principals(owner),  # type: ignore[arg-type]
            evidence=env.evidence,
            preferences=env.preferences,
        )
    }
    ctx = await env.access.resolver.context_for_run(owner, run)
    outcome = await specs[READ_REPORT].handler(
        ReadReportInput(report_id=report_id), OperationContext(ctx, _id("op"))
    )
    assert isinstance(outcome, ToolSucceeded)
    (cited,) = outcome.output.evidence
    assert cited.reusable and cited.not_reusable_reason is None
    assert cited.source is not None and "historical snapshot" in cited.source
    assert "48213.75" in await env.cite(owner, run, evidence_id)

    # Definition change: the report still reads, its figures are not reused.
    await env.db.evidence.invalidate_dependent_findings(owner.executive_id, None, SLOT)
    _, run2 = await env.new_session_run(owner)
    ctx2 = await env.access.resolver.context_for_run(owner, run2)
    again = await specs[READ_REPORT].handler(
        ReadReportInput(report_id=report_id), OperationContext(ctx2, _id("op"))
    )
    assert isinstance(again, ToolSucceeded)
    assert again.output.markdown
    (stale,) = again.output.evidence
    assert not stale.reusable and stale.not_reusable_reason == "invalidated"
