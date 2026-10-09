"""Reports saved before demographics became aggregate-only (T09-F1).

A version citing legacy individual-level demographic evidence cannot be read,
exported or searched, and is listed without its title; the saved artifact
and evidence stay untouched. Versions citing verifiably group-level legacy
evidence remain readable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from retail_analytics.application.contracts.reports import ReportAccess
from retail_analytics.application.evidence_privacy import (
    EvidencePrivacyScreen,
    PrivacyVerdict,
)
from retail_analytics.application.output_privacy import OutputWithheld
from retail_analytics.domain.evidence import (
    CurrentAuthority,
    Evidence,
    ReuseBlock,
)
from retail_analytics.domain.reports import ReportError, ReportErrorCode
from tests.unit.context.support import A
from tests.unit.context.test_legacy_demographics import (
    AGGREGATE_COLUMNS,
    AGGREGATE_SQL,
    PROFILE_COLUMNS,
    PROFILE_SQL,
    REF,
)
from tests.unit.reports.support import ReportWorld, draft

pytestmark = pytest.mark.asyncio


class _OldPolicy(EvidencePrivacyScreen):
    """The rule before T09-F1: every record was shown."""

    def verdict(self, evidence: Evidence) -> PrivacyVerdict:
        return PrivacyVerdict.SAFE


@pytest.fixture
def w(tmp_path: Path) -> ReportWorld:
    return ReportWorld(tmp_path / "artifacts")


async def _saved_under_old_policy(w: ReportWorld, *, profile: bool) -> tuple[str, str]:
    run = w.new_run()
    record = await w.legacy(
        run,
        sql=PROFILE_SQL if profile else AGGREGATE_SQL,
        columns=PROFILE_COLUMNS if profile else AGGREGATE_COLUMNS,
        rows=((REF, "CA", "25-29"),) if profile else (("CA", 2),),
    )
    current = w.evidence._privacy
    w.evidence._privacy = _OldPolicy()
    w.evidence._privacy_verdicts.clear()
    try:
        saved = await w.reports.create(
            A,
            run,
            draft(record.evidence_id, title="Customers by state"),
            operation_id=w.op(),
        )
    finally:
        w.evidence._privacy = current
        w.evidence._privacy_verdicts.clear()
    return saved.version.report_id, record.evidence_id


async def test_legacy_profile_report_cannot_be_read_exported_or_searched(
    w: ReportWorld,
) -> None:
    report_id, evidence_id = await _saved_under_old_policy(w, profile=True)

    for open_report in (w.reports.read, w.reports.export):
        with pytest.raises(ReportError) as caught:
            await open_report(A, report_id)
        assert caught.value.code is ReportErrorCode.EVIDENCE_UNAVAILABLE
        assert "earlier privacy rule" in caught.value.message
        assert REF not in caught.value.message

    (listing,) = await w.reports.list_reports(A)
    assert listing.access is ReportAccess.PRIVACY_WITHDRAWN
    assert listing.title is None
    found = await w.reports.search(A, "state")
    assert found.matches == () and found.withheld == 1

    # Nothing was rewritten or purged: artifact and evidence are retained.
    version = await w.repository.get(A.executive_id, report_id)
    assert version is not None
    content = await w.artifacts.read(
        A.executive_id, report_id, version.artifact_version
    )
    assert content.content
    stored = await w.store.get(evidence_id)
    assert stored is not None and stored.evidence.is_intact


async def test_group_level_legacy_report_stays_readable(w: ReportWorld) -> None:
    report_id, _ = await _saved_under_old_policy(w, profile=False)
    document = await w.reports.read(A, report_id)
    assert "Customers by state" in document.markdown
    (listing,) = await w.reports.list_reports(A)
    assert listing.access is ReportAccess.AVAILABLE
    assert listing.title == "Customers by state"


async def test_new_reports_cannot_cite_legacy_profiles(w: ReportWorld) -> None:
    run = w.new_run()
    record = await w.legacy(
        run, sql=PROFILE_SQL, columns=PROFILE_COLUMNS, rows=((REF, "CA", "25-29"),)
    )
    with pytest.raises(OutputWithheld):
        await w.reports.create(A, run, draft(record.evidence_id), operation_id=w.op())


async def test_imports_into_other_sessions_are_refused(w: ReportWorld) -> None:
    run = w.new_run()
    record = await w.legacy(
        run, sql=PROFILE_SQL, columns=PROFILE_COLUMNS, rows=((REF, "CA", "25-29"),)
    )
    scope = w.scope()
    block = w.evidence.policy.imported_block(
        record,
        CurrentAuthority(A.executive_id, "another-session", scope),
        covered=True,
        invalidated=False,
        privacy_withdrawn=await w.evidence.privacy_withdrawn(record),
    )
    assert block is ReuseBlock.PRIVACY_POLICY_WITHDRAWN
    assert block.is_authority
