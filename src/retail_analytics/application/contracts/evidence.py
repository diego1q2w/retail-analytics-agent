from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from retail_analytics.domain.evidence import (
    AuthorityStamp,
    Evidence,
    EvidenceContent,
    EvidenceUse,
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
    # Earlier evidence this one refreshes: same lineage, next version.
    refreshes: str | None = None


@dataclass(frozen=True, slots=True)
class RunEvidenceLink:
    run_id: str
    evidence_id: str
    use: EvidenceUse
    linked_at: datetime
