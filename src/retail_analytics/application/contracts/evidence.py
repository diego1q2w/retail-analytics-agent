from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from retail_analytics.domain.evidence import (
    AuthorityStamp,
    Evidence,
    EvidenceContent,
    EvidenceUse,
    ReportSource,
    ReuseBlock,
)

DEFAULT_CANDIDATE_LIMIT = 20


@dataclass(frozen=True, slots=True)
class StoredEvidence:
    evidence: Evidence
    invalidated: bool = False
    # Derived (through records of its own session) from saved-report evidence
    # of another session that no longer has a valid link into this session.
    source_withdrawn: bool = False


@dataclass(frozen=True, slots=True)
class NewEvidence:
    """A record to store. The repository assigns lineage and version."""

    evidence_id: str
    executive_id: str
    session_id: str
    run_id: str
    operation_id: str
    authority: AuthorityStamp
    content: EvidenceContent
    computed_at: datetime
    content_digest: str
    # The exact product set behind ``authority.scope_digest``, from the trusted
    # execution context. The store keeps it apart from the record (a scope
    # snapshot keyed by digest) for report access checks; it is never part of
    # the evidence returned to callers or shown to the model.
    scope_products: frozenset[str]
    # Earlier evidence this one refreshes: same lineage, next version.
    refreshes: str | None = None


@dataclass(frozen=True, slots=True)
class RunEvidenceLink:
    run_id: str
    evidence_id: str
    use: EvidenceUse
    linked_at: datetime


@dataclass(frozen=True, slots=True)
class NewEvidenceImport:
    """Link the owner's saved-report evidence into one of their sessions."""

    session_id: str
    evidence_id: str
    executive_id: str
    # The run that linked it (also linked to it as ``reused``).
    run_id: str
    report_id: str
    report_version: int
    report_title: str


@dataclass(frozen=True, slots=True)
class ImportedEvidence:
    """Report evidence linked into a session, with where it came from.

    ``invalidated`` covers both the record itself and a later change of an
    analytical setting scoped to the importing session. A record linked
    through several reports is returned once, with a live link's source when
    it has one; ``withdrawn`` means every link was withdrawn (its reports
    were soft-deleted, or restored and not re-validated).
    """

    evidence: Evidence
    source: ReportSource
    invalidated: bool = False
    withdrawn: bool = False


# Link states after a report deletion (T18-F5).
WITHDRAWN_REPORT_DELETED = "report_deleted"
WITHDRAWN_REVALIDATION_PENDING = "revalidation_pending"
WITHDRAWN_REVALIDATION_FAILED = "revalidation_failed"
REVALIDATION_REINSTATED = "reinstated"
REVALIDATION_REIMPORTED = "reimported"


@dataclass(frozen=True, slots=True)
class PendingReuseLink:
    """A restored report's link into a session, awaiting re-validation."""

    session_id: str
    evidence_id: str
    report_version: int
    # A setting scoped to that session changed since the import.
    superseded: bool = False


@dataclass(frozen=True, slots=True)
class ReuseLinkVerdict:
    """Re-validation result for one link; ``block`` None reinstates it."""

    session_id: str
    evidence_id: str
    block: ReuseBlock | None


@dataclass(frozen=True, slots=True)
class ReuseRevalidation:
    """Recorded outcome of re-validating a restored report's reuse links
    (counts only; the audit trail holds the same)."""

    report_id: str
    reinstated: int = 0
    refused: Mapping[str, int] = field(default_factory=dict)

    @property
    def refused_total(self) -> int:
        return sum(self.refused.values())
