"""Saved reports: create, version, read, export, list and search."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from retail_analytics.application.authorization import AccessDenied
from retail_analytics.application.contracts.authorization import Principal
from retail_analytics.application.contracts.reports import ReportAccess
from retail_analytics.application.output_privacy import OutputWithheld
from retail_analytics.domain.access import Permission
from retail_analytics.domain.evidence import PinHolder
from retail_analytics.domain.reports import (
    ActionItem,
    Finding,
    ReportDraft,
    ReportError,
    ReportErrorCode,
)
from tests.unit.context.support import NARROW, WIDE, A, B
from tests.unit.privacy.support import EXEC_A
from tests.unit.reports.support import ReportWorld, draft

pytestmark = pytest.mark.asyncio


@pytest.fixture
def w(tmp_path: Path) -> ReportWorld:
    return ReportWorld(tmp_path / "artifacts")


async def saved(w: ReportWorld, run: str | None = None):  # type: ignore[no-untyped-def]
    run = run or w.new_run()
    evidence = await w.product_evidence(run)
    result = await w.reports.create(
        A, run, draft(evidence.evidence_id), operation_id=w.op()
    )
    return run, evidence, result


async def test_report_is_listed_reopened_and_exported_with_cited_evidence(
    w: ReportWorld,
) -> None:
    _, evidence, result = await saved(w)
    assert result.version.version == 1 and not result.duplicate
    assert result.version.evidence_ids == (evidence.evidence_id,)

    (listing,) = await w.reports.list_reports(A)
    assert (listing.report_id, listing.title) == (
        result.version.report_id,
        "September revenue by product",
    )

    document = await w.reports.read(A, listing.report_id)
    assert f"[{evidence.evidence_id}]" in document.markdown
    assert "## Findings" in document.markdown
    assert document.evidence[0].evidence_id == evidence.evidence_id

    exported = await w.reports.export(A, listing.report_id)
    text = exported.content.decode()
    assert exported.filename.endswith("-v1.md")
    assert text.startswith("# September revenue by product")
    assert "| 101 | Unnamed product | Acme | 10 |" in text
    assert "| 102 | Unnamed product | Unknown brand | 20 |" in text
    assert "| 103 | Shirt | Zed | 30 |" in text


async def test_missing_labels_are_display_only_and_ids_stay_distinct(
    w: ReportWorld,
) -> None:
    _, evidence, result = await saved(w)
    document = await w.reports.read(A, result.version.report_id)
    rows = document.evidence[0].rows
    assert [r[0] for r in rows] == ["101", "102", "103"]
    assert rows[0][1] == rows[1][1] == "Unnamed product"
    assert rows[1][2] == "Unknown brand"
    stored = (await w.evidence.owned_records(EXEC_A, [evidence.evidence_id]))[0]
    assert stored.content.table.rows[0][1] is None
    assert stored.content.table.rows[1][2] is None
    assert stored.is_intact


async def test_body_states_basis_and_separates_recommendations_from_findings(
    w: ReportWorld,
) -> None:
    _, _, result = await saved(w)
    markdown = (await w.reports.read(A, result.version.report_id)).markdown
    findings, _rest = markdown.split("## Findings")[1].split("## Definitions", 1)
    assert "Recommendation" not in findings
    actions = markdown.split("## Recommended actions")[1].split("## Evidence")[0]
    assert "**Recommendation:** Ask the catalog team" in actions
    assert "not observed results" in actions
    basis = markdown.split("## Evidence and data basis")[1]
    assert "2026-09-01 to 2026-09-30 inclusive" in basis
    assert "orders.created_at" in basis and "completed_item_sales@1" in basis


async def test_duplicate_operation_returns_the_original_without_a_second_report(
    w: ReportWorld,
) -> None:
    run = w.new_run()
    evidence = await w.product_evidence(run)
    d = draft(evidence.evidence_id)
    first = await w.reports.create(A, run, d, operation_id="op-1")
    again = await w.reports.create(A, run, d, operation_id="op-1")
    assert again.duplicate and again.version == first.version
    assert len(w.repository.rows) == 1 and len(w.catalog.rows) == 1
    with pytest.raises(ReportError) as conflict:
        await w.reports.create(
            A, run, draft(evidence.evidence_id, title="Another"), operation_id="op-1"
        )
    assert conflict.value.code is ReportErrorCode.IDEMPOTENCY_CONFLICT


async def test_new_version_preserves_the_prior_content(w: ReportWorld) -> None:
    run, evidence, first = await saved(w)
    report_id = first.version.report_id
    second = await w.reports.create(
        A,
        run,
        draft(evidence.evidence_id, title="September revenue, revised"),
        operation_id=w.op(),
        report_id=report_id,
        base_version=1,
    )
    assert second.version.version == 2 and second.version.report_id == report_id
    old = await w.reports.read(A, report_id, 1)
    new = await w.reports.read(A, report_id)
    assert old.markdown.startswith("# September revenue by product")
    assert new.markdown.startswith("# September revenue, revised")
    assert [v.version for v in await w.reports.versions(A, report_id)] == [1, 2]
    with pytest.raises(ReportError) as stale:
        await w.reports.create(
            A, run, draft(evidence.evidence_id, title="Late"), operation_id=w.op(),
            report_id=report_id, base_version=1,
        )  # fmt: skip
    assert stale.value.code is ReportErrorCode.STALE_BASE_VERSION


async def test_evidence_is_pinned_for_the_report(w: ReportWorld) -> None:
    _, evidence, result = await saved(w)
    assert (
        evidence.evidence_id,
        PinHolder("report", result.version.report_id),
    ) in w.store.pins


async def test_other_executives_cannot_read_export_list_search_or_overwrite(
    w: ReportWorld,
) -> None:
    _, evidence, result = await saved(w)
    report_id = result.version.report_id
    for call in (
        w.reports.read(B, report_id),
        w.reports.export(B, report_id),
        w.reports.versions(B, report_id),
    ):
        with pytest.raises(AccessDenied):
            await call
    assert await w.reports.list_reports(B) == ()
    assert (await w.reports.search(B, "September")).matches == ()
    run_b = w.new_run("demo-b", "s-b")
    with pytest.raises(AccessDenied):
        await w.reports.create(
            B,
            run_b,
            draft(evidence.evidence_id),
            operation_id=w.op(),
            report_id=report_id,
        )
    # An artifact of the report cannot be fetched directly either.
    with pytest.raises(AccessDenied):
        await w.artifacts.read("demo-b", report_id)


async def test_unknown_or_foreign_evidence_cannot_be_cited(w: ReportWorld) -> None:
    run = w.new_run()
    foreign_run = w.new_run("demo-b", "s-b")
    foreign = await w.product_evidence(foreign_run, principal=B)
    for evidence_id in ("evd_missing", foreign.evidence_id):
        with pytest.raises(AccessDenied):
            await w.reports.create(A, run, draft(evidence_id), operation_id=w.op())
    assert w.repository.rows == [] and w.catalog.rows == []


async def test_evidence_that_became_inaccessible_discards_the_report(
    w: ReportWorld,
) -> None:
    run = w.new_run()
    evidence = await w.product_evidence(run)
    w.set_products(EXEC_A, NARROW)  # entitlement change after the analysis
    with pytest.raises(OutputWithheld) as withheld:
        await w.reports.create(A, run, draft(evidence.evidence_id), operation_id=w.op())
    assert withheld.value.reason == "unavailable_evidence"
    assert w.repository.rows == [] and w.catalog.rows == []
    assert not any(h.kind == "report" for _, h in w.store.pins)


async def test_personal_data_blocks_the_save_and_nothing_is_stored(
    w: ReportWorld,
) -> None:
    run = w.new_run()
    evidence = await w.product_evidence(run)
    bad = replace(
        draft(evidence.evidence_id),
        summary="The top buyer is dave@example.invalid.",
    )
    with pytest.raises(OutputWithheld) as withheld:
        await w.reports.create(A, run, bad, operation_id=w.op())
    assert withheld.value.reason == "personal_data"
    assert w.repository.rows == [] and w.catalog.rows == []


async def test_text_cannot_cite_evidence_the_report_does_not_declare() -> None:
    with pytest.raises(ReportError) as err:
        ReportDraft(
            "t",
            "See evd_deadbeef for details.",
            (Finding("Revenue grew.", ("evd_aaaa",)),),
        )
    assert err.value.code is ReportErrorCode.UNDECLARED_CITATION
    with pytest.raises(ReportError):
        Finding("No evidence behind this.", ())
    with pytest.raises(ReportError):
        ReportDraft("t", "# Fake heading", (Finding("x", ("evd_a",)),))
    assert ActionItem("Recommend something").based_on == ()


async def test_access_change_withholds_content_titles_and_search_hits(
    w: ReportWorld,
) -> None:
    _, _, result = await saved(w)
    report_id = result.version.report_id
    w.set_products(EXEC_A, NARROW)
    with pytest.raises(ReportError) as read:
        await w.reports.read(A, report_id)
    assert read.value.code is ReportErrorCode.ACCESS_CHANGED
    with pytest.raises(ReportError):
        await w.reports.export(A, report_id)
    (listing,) = await w.reports.list_reports(A)
    assert listing.title is None and listing.access is ReportAccess.ACCESS_CHANGED
    found = await w.reports.search(A, "September")
    assert found.matches == () and found.withheld == 1
    # Same product set again (only the entitlement counter moved): readable.
    w.set_products(EXEC_A, WIDE)
    assert (await w.reports.read(A, report_id)).version.version == 1
    assert len((await w.reports.search(A, "September")).matches) == 1


async def test_search_matches_title_and_content_for_the_owner_only(
    w: ReportWorld,
) -> None:
    await saved(w)
    other_run = w.new_run(session="s-a")
    other_evidence = await w.product_evidence(other_run)
    await w.reports.create(
        A,
        other_run,
        replace(
            draft(other_evidence.evidence_id, title="Returns review"),
            summary="Returns rose among accessories.",
        ),
        operation_id=w.op(),
    )
    by_title = await w.reports.search(A, "returns review")
    assert [m.matched_in for m in by_title.matches] == ["title"]
    by_content = await w.reports.search(A, "accessories")
    assert [m.listing.title for m in by_content.matches] == ["Returns review"]
    assert "accessories" in by_content.matches[0].snippet
    assert (await w.reports.search(A, "no such phrase anywhere")).matches == ()
    assert (await w.reports.search(B, "accessories")).matches == ()
    with pytest.raises(ReportError):
        await w.reports.search(A, "   ")


async def test_conversation_filter_and_deleted_reports(w: ReportWorld) -> None:
    _, _, first = await saved(w)
    w.records.sessions["s-a2"] = replace(w.records.sessions["s-a"], session_id="s-a2")
    second = (await saved(w, w.new_run(session="s-a2")))[2]
    first_id, second_id = first.version.report_id, second.version.report_id
    both = await w.reports.list_reports(A)
    assert {r.report_id for r in both} == {first_id, second_id}
    only = await w.reports.list_reports(A, session_id="s-a2")
    assert [r.report_id for r in only] == [second_id]
    assert await w.reports.list_reports(A, session_id="other") == ()
    found = await w.reports.search(A, "September", session_id="s-a")
    assert [m.listing.report_id for m in found.matches] == [first_id]
    w.repository.deleted.add(first_id)  # soft-deleted by the deletion flow
    assert [r.report_id for r in await w.reports.list_reports(A)] == [second_id]
    assert [
        m.listing.report_id for m in (await w.reports.search(A, "September")).matches
    ] == [second_id]
    with pytest.raises(AccessDenied):
        await w.reports.read(A, first_id)


async def test_reading_requires_the_read_permission(w: ReportWorld) -> None:
    _, _, result = await saved(w)
    limited = Principal(EXEC_A, frozenset({Permission.ANALYSIS_READ.value}))
    with pytest.raises(AccessDenied):
        await w.reports.read(limited, result.version.report_id)
