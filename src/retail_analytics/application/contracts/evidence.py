from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from retail_analytics.domain.evidence import (
    AuthorityStamp,
    Evidence,
    EvidenceContent,
    EvidenceUse,
    ReportSource,
)

DEFAULT_CANDIDATE_LIMIT = 20


@dataclass(frozen=True, slots=True)
class StoredEvidence:
    evidence: Evidence
    invalidated: bool = False


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
    analytical setting scoped to the importing session.
    """

    evidence: Evidence
    source: ReportSource
    invalidated: bool = False
