"""Saved, read and exported reports show raw figures rounded; evidence keeps
full precision and missing values never become numbers."""

from __future__ import annotations

from pathlib import Path

import pytest

from retail_analytics.domain.reports import Finding, ReportDraft
from tests.unit.context.support import A
from tests.unit.privacy.support import EXEC_A
from tests.unit.reports.support import ReportWorld

pytestmark = pytest.mark.asyncio

RAW = 1336.5899896621704
ROWS: tuple[tuple[object, ...], ...] = (
    (101, "Coat", "Acme", RAW),
    (102, "Hat", "Acme", -12.3456789),
    (103, "Sock", "Acme", 0.0041234),
    (104, "Belt", "Acme", None),
)


@pytest.fixture
def w(tmp_path: Path) -> ReportWorld:
    return ReportWorld(tmp_path / "artifacts")


async def test_report_text_and_rows_are_rounded(w: ReportWorld) -> None:
    run = w.new_run()
    evidence = await w.product_evidence(run, rows=ROWS)
    cited = (evidence.evidence_id,)
    report = ReportDraft(
        title="September revenue",
        summary=f"Product 101 earned {RAW} (exact figure: {RAW}).",
        findings=(Finding("Product 102 had returns of -12.3456789.", cited),),
        definitions=("Revenue means completed item sales.",),
        limitations=("Product 104 has no recorded revenue.",),
    )
    saved = await w.reports.create(A, run, report, operation_id=w.op())

    markdown = (await w.reports.read(A, saved.version.report_id)).markdown
    assert "Product 101 earned 1,336.59 (exact figure: 1,336.59)." in markdown
    assert "returns of -12.35." in markdown
    assert "1336.58998" not in markdown

    exported = (await w.reports.export(A, saved.version.report_id)).content.decode()
    assert "| 101 | Coat | Acme | 1,336.59 |" in exported
    assert "| 102 | Hat | Acme | -12.35 |" in exported
    # A small nonzero amount keeps significant digits instead of 0.00.
    assert "| 103 | Sock | Acme | 0.00412 |" in exported
    # Missing stays missing.
    assert "| 104 | Belt | Acme | (none) |" in exported
    assert "0.00 |" not in exported

    (stored,) = await w.evidence.owned_records(EXEC_A, [evidence.evidence_id])
    assert stored.content.table.rows[0][3] == RAW
