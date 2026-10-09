"""Display-time notices when a report's definitions differ from current ones
(T18-F3). Reading is never blocked; the saved report never changes."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from retail_analytics.application.contracts.authorization import Principal
from retail_analytics.application.contracts.tools import OperationContext
from retail_analytics.application.reports import ReportService
from retail_analytics.application.tools import ToolSucceeded
from retail_analytics.capabilities.reports import (
    EXPORT_REPORT,
    READ_REPORT,
    ExportReportInput,
    ReadReportInput,
    report_capabilities,
)
from retail_analytics.domain.evidence import (
    AnalysisStamp,
    DefinitionRef,
    TermMeaning,
)
from retail_analytics.domain.metrics import (
    COMPLETE_STATUS,
    COMPLETED_ITEM_SALES,
    MetricCatalog,
    PopulationFilter,
    builtin_metrics,
    default_catalog,
)
from retail_analytics.domain.periods import DateWindow, OverrideScope
from retail_analytics.domain.preferences import PreferenceSetting, PreferenceSource
from retail_analytics.domain.report_definitions import (
    UNRECORDED_MESSAGE,
    DefinitionNoticeKind,
)
from retail_analytics.domain.reports import ReportError, ReportErrorCode
from tests.unit.context.support import NARROW, A
from tests.unit.evidence.support import FINGERPRINT
from tests.unit.privacy.support import EXEC_A
from tests.unit.reports.support import ReportWorld, draft

pytestmark = pytest.mark.asyncio

REVENUE_V1 = DefinitionRef(COMPLETED_ITEM_SALES, 1)
SHIPPED = "shipped_item_sales"
SEPTEMBER = DateWindow(date(2026, 9, 1), date(2026, 10, 1))


def _with_shipped(metric_id: str, version: int) -> MetricCatalog:
    """The catalog plus ``metric_id``@``version``. A new version of an existing
    metric keeps its population (ratios built on it must stay consistent);
    a new metric counts shipped items too."""
    base = default_catalog().get(COMPLETED_ITEM_SALES)
    population = (
        base.population
        if metric_id == COMPLETED_ITEM_SALES
        else PopulationFilter("item_status", frozenset({COMPLETE_STATUS, "Shipped"}))
    )
    extra = replace(
        base,
        metric_id=metric_id,
        version=version,
        description="Sum of sale_amount over the qualifying items.",
        population=population,
    )
    return MetricCatalog((*builtin_metrics(), extra))


def recorded(
    *,
    definitions: frozenset[DefinitionRef] = frozenset({REVENUE_V1}),
    terms: bool = True,
) -> AnalysisStamp:
    return AnalysisStamp(
        catalog_version=1,
        policy_version=1,
        definitions=definitions,
        preference_fingerprint=FINGERPRINT,
        period=SEPTEMBER,
        terms=frozenset({TermMeaning("revenue", REVENUE_V1)}) if terms else frozenset(),
        date_basis="ordered_date",
        definitions_recorded=True,
    )


@pytest.fixture
def w(tmp_path: Path) -> ReportWorld:
    return ReportWorld(tmp_path / "artifacts")


def service(w: ReportWorld, catalog: MetricCatalog) -> ReportService:
    return ReportService(
        w.repository,
        w.artifacts,
        w.evidence,
        w.gate,
        w.resolver,
        catalog,
        w.store,
        w.preference_store,
    )


async def save(w: ReportWorld, analysis: AnalysisStamp | None) -> str:
    run = w.new_run()
    evidence = await w.product_evidence(run, analysis=analysis)
    saved = await w.reports.create(
        A, run, draft(evidence.evidence_id), operation_id=w.op()
    )
    return saved.version.report_id


async def remember_revenue(
    w: ReportWorld, metric_id: str, version: int, *, session_id: str | None = None
) -> None:
    await w.preference_store.save(
        EXEC_A,
        PreferenceSetting.metric("revenue", metric_id, version),
        OverrideScope.SESSION if session_id else OverrideScope.USER_DEFAULT,
        session_id,
        PreferenceSource.EXPLICIT,
    )


async def test_matching_definitions_have_no_notice(w: ReportWorld) -> None:
    report_id = await save(w, recorded())
    assert (await w.reports.read(A, report_id)).notices == ()
    assert (await w.reports.export(A, report_id)).notices == ()


async def test_changed_term_definition_names_both_and_needs_recalculation(
    w: ReportWorld,
) -> None:
    report_id = await save(w, recorded())
    catalog = _with_shipped(SHIPPED, 1)
    await remember_revenue(w, SHIPPED, 1)

    (notice,) = (await service(w, catalog).read(A, report_id)).notices

    assert notice.kind is DefinitionNoticeKind.DEFINITION_CHANGED
    assert notice.subject == "revenue"
    assert notice.report_definition is not None
    assert notice.current_definition is not None
    assert "completed item sales (version 1)" in notice.report_definition
    assert "status Complete," in notice.report_definition
    assert "shipped item sales (version 1)" in notice.current_definition
    assert "Complete or Shipped" in notice.current_definition
    assert notice.message.startswith(
        "The definitions recorded for this report's evidence include "
        '"revenue" as completed item sales (version 1)'
    )
    assert notice.current_definition in notice.message
    assert "have not been recalculated" in notice.message
    assert "requires recalculating" in notice.message
    assert notice.recalculation_required
    exported = await service(w, catalog).export(A, report_id)
    assert exported.notices == (notice,)


async def test_new_metric_version_is_a_change_unless_the_user_pins_the_old(
    w: ReportWorld,
) -> None:
    report_id = await save(w, recorded())
    catalog = _with_shipped(COMPLETED_ITEM_SALES, 2)

    (notice,) = (await service(w, catalog).read(A, report_id)).notices
    assert "completed item sales (version 1)" in notice.message
    assert "completed item sales (version 2)" in notice.message

    await remember_revenue(w, COMPLETED_ITEM_SALES, 1)
    assert (await service(w, catalog).read(A, report_id)).notices == ()


async def test_metric_without_a_term_reports_its_version_change(
    w: ReportWorld,
) -> None:
    report_id = await save(w, recorded(terms=False))
    (notice,) = (
        await service(w, _with_shipped(COMPLETED_ITEM_SALES, 2)).read(A, report_id)
    ).notices
    assert notice.subject == COMPLETED_ITEM_SALES
    assert "The current definition is completed item sales (version 2)" in (
        notice.message
    )


async def test_session_preference_applies_only_to_that_session(
    w: ReportWorld,
) -> None:
    report_id = await save(w, recorded())
    catalog = _with_shipped(SHIPPED, 1)
    await remember_revenue(w, SHIPPED, 1, session_id="s-a")

    assert (await service(w, catalog).read(A, report_id)).notices == ()
    in_session = await service(w, catalog).read(A, report_id, session_id="s-a")
    assert len(in_session.notices) == 1


async def test_unrecorded_definitions_get_a_neutral_notice(w: ReportWorld) -> None:
    report_id = await save(w, None)  # evidence saved before T18-F3

    (notice,) = (await w.reports.read(A, report_id)).notices

    assert notice.kind is DefinitionNoticeKind.DEFINITIONS_NOT_RECORDED
    assert notice.message == UNRECORDED_MESSAGE
    assert "definitions relevant to this report were not recorded" in notice.message
    assert notice.report_definition is None and notice.current_definition is None


async def test_notices_never_change_the_saved_report(w: ReportWorld) -> None:
    report_id = await save(w, recorded())
    record = (await w.reports.versions(A, report_id))[0]
    before = await w.artifacts.read(EXEC_A, report_id, record.version)
    catalog = _with_shipped(SHIPPED, 1)
    await remember_revenue(w, SHIPPED, 1)
    reports = service(w, catalog)

    document = await reports.read(A, report_id)
    exported = await reports.export(A, report_id)

    after = await w.artifacts.read(EXEC_A, report_id, record.version)
    assert document.notices and exported.notices
    assert after.content == before.content
    assert document.markdown.encode() == before.content
    assert exported.content.startswith(before.content.rstrip())
    assert b"recalculat" not in exported.content
    assert (await w.reports.versions(A, report_id))[0] == record


async def test_access_rules_still_apply_with_notices(w: ReportWorld) -> None:
    report_id = await save(w, recorded())
    await remember_revenue(w, SHIPPED, 1)
    w.set_products(EXEC_A, NARROW)
    with pytest.raises(ReportError) as error:
        await service(w, _with_shipped(SHIPPED, 1)).read(A, report_id)
    assert error.value.code is ReportErrorCode.ACCESS_CHANGED


class _Principals:
    async def get(self, run_id: str) -> Principal:
        return A


async def test_read_and_export_tools_show_notices(w: ReportWorld) -> None:
    report_id = await save(w, None)
    specs = {
        s.name: s
        for s in report_capabilities(
            w.reports,
            principals=_Principals(),  # type: ignore[arg-type]
            evidence=w.evidence,
            preferences=w.preferences,
        )
    }
    ctx = await w.resolver.context_for_run(A, w.new_run())

    read = await specs[READ_REPORT].handler(
        ReadReportInput(report_id=report_id), OperationContext(ctx, w.op())
    )
    exported = await specs[EXPORT_REPORT].handler(
        ExportReportInput(report_id=report_id), OperationContext(ctx, w.op())
    )

    assert isinstance(read, ToolSucceeded) and isinstance(exported, ToolSucceeded)
    (notice,) = read.output.definition_notices
    assert notice.kind == "definitions_not_recorded"
    assert notice.message == UNRECORDED_MESSAGE
    assert exported.output.definition_notices == (notice,)
    # Reading is not blocked; reuse of unknown definitions is refused
    # (tests/unit/evidence/test_report_reuse.py).
    assert read.output.markdown.startswith("# September revenue by product")
