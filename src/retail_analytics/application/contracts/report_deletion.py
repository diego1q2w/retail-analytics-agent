from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from retail_analytics.domain.report_deletion import ProposalStatus


@dataclass(frozen=True, slots=True)
class NewDeletionProposal:
    proposal_id: str
    audit_id: str
    owner_id: str
    session_id: str | None
    run_id: str | None
    report_ids: tuple[str, ...]
    idempotency_key: str
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class PreviewItem:
    """``title`` is None when the owner's product access changed since the
    report was saved: its content stays withheld, but it can still be deleted."""

    report_id: str
    version: int
    title: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class DeletionPreview:
    proposal_id: str
    status: ProposalStatus
    expires_at: datetime
    items: tuple[PreviewItem, ...]
    # True when a retried operation returned a proposal made earlier.
    duplicate: bool = False

    @property
    def count(self) -> int:
        return len(self.items)


@dataclass(frozen=True, slots=True)
class DeletionResult:
    proposal_id: str
    report_ids: tuple[str, ...]
    deleted_at: datetime
    # An operator can restore the reports until this moment.
    recoverable_until: datetime
