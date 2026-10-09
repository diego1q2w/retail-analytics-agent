"""A world for report tests: the context-selection world plus report services."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from retail_analytics.adapters.filesystem.blobs import LocalBlobStore
from retail_analytics.application.artifacts import ArtifactPolicy, ArtifactService
from retail_analytics.application.contracts.authorization import Principal
from retail_analytics.application.contracts.tools import OperationContext
from retail_analytics.application.reports import ReportService
from retail_analytics.domain.evidence import (
    AnalysisStamp,
    Evidence,
    EvidenceColumn,
    EvidenceContent,
    EvidenceKind,
    EvidenceTable,
    Provenance,
)
from retail_analytics.domain.metrics import default_catalog
from retail_analytics.domain.periods import DateWindow
from retail_analytics.domain.reports import ActionItem, Finding, ReportDraft
from tests.unit.context.support import A, World
from tests.unit.evidence.support import FINGERPRINT, REVENUE
from tests.unit.reports.fakes import FakeReportRepository
from tests.unit.test_artifacts import MemoryCatalog

PRODUCT_COLUMNS = (
    EvidenceColumn("product_id", "value", ("products.product_id",)),
    EvidenceColumn("product_name", "value", ("products.product_name",)),
    EvidenceColumn("brand", "value", ("products.brand",)),
    EvidenceColumn("revenue", "value", ("sales_items.sale_amount",)),
)
PRODUCT_ROWS: tuple[tuple[object, ...], ...] = (
    (101, None, "Acme", 10),
    (102, None, None, 20),
    (103, "Shirt", "Zed", 30),
)


class ReportWorld(World):
    def __init__(self, root: Path) -> None:
        super().__init__()
        self.repository = FakeReportRepository(self.clock)
        self.catalog = MemoryCatalog()
        self.artifacts = ArtifactService(
            self.catalog,
            LocalBlobStore(root),
            ArtifactPolicy.with_limits(markdown=64 * 1024, binary=4096),
        )
        self.reports = ReportService(
            self.repository,
            self.artifacts,
            self.evidence,
            self.gate,
            self.resolver,
            default_catalog(),
            self.store,
            self.preference_store,
        )
        self._counter = 0

    def op(self) -> str:
        self._counter += 1
        return f"report-op-{self._counter}"

    async def product_evidence(
        self,
        run_id: str,
        rows: tuple[tuple[object, ...], ...] = PRODUCT_ROWS,
        principal: Principal = A,
        analysis: AnalysisStamp | None = None,
    ) -> Evidence:
        ctx = await self.resolver.context_for_run(principal, run_id)
        content = EvidenceContent(
            kind=EvidenceKind.EXTERNAL,
            subject_key=f"p:{self.op()}",
            analysis=analysis
            or AnalysisStamp(
                catalog_version=1,
                policy_version=1,
                definitions=frozenset({REVENUE}),
                preference_fingerprint=FINGERPRINT,
                period=DateWindow(date(2026, 9, 1), date(2026, 10, 1)),
            ),
            provenance=Provenance(notes=(("source", "fixture"),)),
            table=EvidenceTable(
                columns=PRODUCT_COLUMNS,
                rows=rows,  # type: ignore[arg-type]
                received_rows=len(rows),
            ),
            grain=("product_id",),
            analytical_slots=frozenset({"metric_definition:revenue"}),
        )
        return await self.evidence.record(OperationContext(ctx, self.op()), content)


def draft(
    *evidence_ids: str, title: str = "September revenue by product"
) -> ReportDraft:
    first = evidence_ids[0]
    return ReportDraft(
        title=title,
        summary="Revenue by product for September 2026.",
        findings=(
            Finding("Product 103 earned the most revenue.", (first,)),
            Finding("Two products have no name in the source.", evidence_ids),
        ),
        definitions=("Revenue means completed item sales.",),
        limitations=("Item statuses reflect the current state of the source.",),
        action_items=(
            ActionItem("Ask the catalog team to name products 101 and 102.", (first,)),
        ),
    )
