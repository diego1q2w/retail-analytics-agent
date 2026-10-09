from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from retail_analytics.domain.reports import ReportVersion


class ReportAccess(StrEnum):
    AVAILABLE = "available"
    # The owner's current products no longer cover the version's required
    # scope (or, for a legacy version, differ from it): content is withheld.
    ACCESS_CHANGED = "access_changed"


@dataclass(frozen=True, slots=True)
class NewReportVersion:
    """A version to store. ``expected_latest`` is 0 for a new report."""

    owner_id: str
    report_id: str
    session_id: str | None
    run_id: str | None
    artifact_version: int
    title: str
    evidence_ids: tuple[str, ...]
    scope_digest: str
    authorization_version: int
    draft_digest: str
    idempotency_key: str
    expected_latest: int
    # Digest of the recorded union of the cited evidence's product sets (see
    # ``ReportVersion.required_scope_digest``); None keeps the strict rule.
    required_scope_digest: str | None


@dataclass(frozen=True, slots=True)
class SavedReport:
    version: ReportVersion
    # True when the same operation had already saved this report version.
    duplicate: bool


@dataclass(frozen=True, slots=True)
class ReportListing:
    """One report's latest version. ``title`` is None when access changed."""

    report_id: str
    version: int
    title: str | None
    created_at: datetime
    session_id: str | None
    evidence_count: int
    access: ReportAccess


@dataclass(frozen=True, slots=True)
class ReportMatch:
    listing: ReportListing
    matched_in: str
    snippet: str


@dataclass(frozen=True, slots=True)
class ReportSearchResult:
    matches: tuple[ReportMatch, ...]
    scanned: int
    # Owned reports that were not searched because their access changed.
    withheld: int
    # True when more live reports exist than the scan limit covers.
    scan_limited: bool


@dataclass(frozen=True, slots=True)
class CitedEvidence:
    """A pinned evidence record as presented with its report."""

    evidence_id: str
    kind: str
    computed_at: datetime
    period_start: date | None
    period_end: date | None
    truncated: bool
    columns: tuple[str, ...]
    # Display text with label fallbacks; the stored evidence keeps NULLs.
    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True, slots=True)
class ReportDocument:
    version: ReportVersion
    markdown: str
    evidence: tuple[CitedEvidence, ...]


@dataclass(frozen=True, slots=True)
class ExportedReport:
    filename: str
    media_type: str
    content: bytes
    version: ReportVersion
