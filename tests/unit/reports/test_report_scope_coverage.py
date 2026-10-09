"""Report access by required-scope coverage (T18-F1), with in-memory stores.

The PostgreSQL behaviour (snapshot store, subset check in SQL, migration
backfill) is in ``tests/integration/test_report_scope_coverage.py``.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from retail_analytics.application.contracts.reports import ReportAccess
from retail_analytics.capabilities.reports import SaveReportInput
from retail_analytics.domain.evidence import product_set_digest, scope_digest
from retail_analytics.domain.reports import ReportError, ReportErrorCode
from tests.unit.context.support import WIDE, A
from tests.unit.privacy.support import EXEC_A
from tests.unit.reports.support import ReportWorld, draft

pytestmark = pytest.mark.asyncio

WIDER = WIDE | {"4", "5"}


@pytest.fixture
def w(tmp_path: Path) -> ReportWorld:
    return ReportWorld(tmp_path / "artifacts")


async def _save(
    w: ReportWorld, rows: tuple[tuple[object, ...], ...] | None = None
) -> str:
    run = w.new_run()
    evidence = (
        await w.product_evidence(run)
        if rows is None
        else await w.product_evidence(run, rows)
    )
    result = await w.reports.create(
        A, run, draft(evidence.evidence_id), operation_id=w.op()
    )
    return result.version.report_id


async def _readable(w: ReportWorld, report_id: str) -> bool:
    (listing,) = await w.reports.list_reports(A)
    search = await w.reports.search(A, "September")
    try:
        await w.reports.read(A, report_id)
        await w.reports.export(A, report_id)
    except ReportError as error:
        assert error.code is ReportErrorCode.ACCESS_CHANGED
        assert listing.title is None
        assert listing.access is ReportAccess.ACCESS_CHANGED
        assert search.matches == () and search.withheld == 1
        return False
    assert listing.access is ReportAccess.AVAILABLE and listing.title is not None
    assert len(search.matches) == 1
    return True


async def test_required_scope_is_the_evidence_stamp_not_the_draft(
    w: ReportWorld,
) -> None:
    report_id = await _save(w)
    version = await w.repository.get(EXEC_A, report_id)
    assert version is not None
    assert version.required_scope_digest == product_set_digest(WIDE)
    assert w.store.snapshots[version.required_scope_digest] == WIDE


async def test_widening_keeps_old_reports_readable(w: ReportWorld) -> None:
    report_id = await _save(w)
    w.set_products(EXEC_A, WIDER)
    assert await _readable(w, report_id)
    (only,) = await w.reports.versions(A, report_id)
    assert only.access is ReportAccess.AVAILABLE


async def test_removing_a_required_product_blocks(w: ReportWorld) -> None:
    report_id = await _save(w)
    w.set_products(EXEC_A, WIDER - {"2"})
    assert not await _readable(w, report_id)


async def test_removing_an_unrelated_product_keeps_access(w: ReportWorld) -> None:
    report_id = await _save(w)
    w.set_products(EXEC_A, WIDER)
    w.set_products(EXEC_A, WIDE | {"5"})  # "4" was never required
    assert await _readable(w, report_id)
    w.set_products(EXEC_A, frozenset({"1", "3", "4"}))  # "2" was
    assert not await _readable(w, report_id)


async def test_products_in_result_rows_do_not_shrink_the_required_scope(
    w: ReportWorld,
) -> None:
    # The rows only mention product 1, but the evidence was computed under
    # products 1-3: keeping only product 1 must not be enough.
    report_id = await _save(w, rows=((1, "Shirt", "Zed", 30),))
    w.set_products(EXEC_A, frozenset({"1"}))
    assert not await _readable(w, report_id)


async def test_the_model_cannot_supply_a_scope() -> None:
    base = {
        "title": "t",
        "summary": "s",
        "findings": [{"text": "f", "evidence_ids": ["evd_1"]}],
    }
    SaveReportInput.model_validate(base)
    for field in ("required_scope", "product_ids", "scope_digest", "product_scope"):
        with pytest.raises(ValidationError):
            SaveReportInput.model_validate({**base, field: ["1"]})


async def test_legacy_version_without_a_recorded_scope_keeps_the_strict_rule(
    w: ReportWorld,
) -> None:
    report_id = await _save(w)
    repo = w.repository
    repo.rows = [replace(r, required_scope_digest=None) for r in repo.rows]
    w.set_products(EXEC_A, WIDER)
    assert not await _readable(w, report_id)
    w.set_products(EXEC_A, WIDE)
    assert await _readable(w, report_id)


async def test_evidence_without_a_snapshot_saves_a_strict_version(
    w: ReportWorld,
) -> None:
    run = w.new_run()
    evidence = await w.product_evidence(run)
    w.store.snapshots.clear()  # stamped before snapshots were recorded
    result = await w.reports.create(
        A, run, draft(evidence.evidence_id), operation_id=w.op()
    )
    assert result.version.required_scope_digest is None
    assert result.version.scope_digest == scope_digest(w.scope())
    w.set_products(EXEC_A, WIDER)
    assert not await _readable(w, result.version.report_id)
