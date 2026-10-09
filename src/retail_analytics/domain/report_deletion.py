"""Exact report-deletion proposals.

A proposal freezes *which* reports (by ID and version) one executive asked to
delete. Confirmation never searches again: it rechecks that exactly those
reports are still the requester's, live and unchanged, and either deletes all
of them or none. Reports created or revised after the proposal are not part of
it. A proposal is single-use and expires ten minutes after it was made.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

PROPOSAL_TTL = timedelta(minutes=10)
MAX_REPORTS_PER_PROPOSAL = 25
MAX_PENDING_PROPOSALS = 20
# Soft-deleted reports stay recoverable by an operator for this long.
RECOVERY_PERIOD = timedelta(days=7)

PROPOSED = "report_deletion.proposed"
CONFIRMED = "report_deletion.confirmed"
CANCELLED = "report_deletion.cancelled"


class ProposalStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"


class DeletionErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    TOO_MANY_PENDING = "too_many_pending"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    EXPIRED = "expired"
    ALREADY_RESOLVED = "already_resolved"
    # A proposed report was deleted, revised or is no longer the requester's.
    STALE = "stale"


class DeletionError(Exception):
    """A deletion step failed. ``message`` is safe to show the caller."""

    def __init__(self, code: DeletionErrorCode, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code.value}: {message}")


@dataclass(frozen=True, slots=True)
class ProposalItem:
    """One proposed report and the version that was current when proposed."""

    report_id: str
    version: int
    title: str
    created_at: datetime
    # Access digests of that version (``ReportVersion``), to judge whether the
    # title may be shown.
    scope_digest: str
    required_scope_digest: str | None = None


@dataclass(frozen=True, slots=True)
class DeletionProposal:
    proposal_id: str
    owner_id: str
    session_id: str | None
    run_id: str | None
    status: ProposalStatus
    created_at: datetime
    expires_at: datetime
    resolved_at: datetime | None
    items: tuple[ProposalItem, ...]

    def expired_at(self, now: datetime) -> bool:
        return now >= self.expires_at


@dataclass(frozen=True, slots=True)
class CurrentReport:
    """A proposed report as it is right now, read under lock."""

    owner_id: str
    latest_version: int
    deleted: bool


def clean_report_ids(report_ids: Sequence[str]) -> tuple[str, ...]:
    unique = tuple(dict.fromkeys(report_ids))
    if not unique:
        raise DeletionError(DeletionErrorCode.INVALID_REQUEST, "no reports were named")
    if len(unique) > MAX_REPORTS_PER_PROPOSAL:
        raise DeletionError(
            DeletionErrorCode.INVALID_REQUEST,
            f"at most {MAX_REPORTS_PER_PROPOSAL} reports can be deleted at once",
        )
    return unique


def request_digest(report_ids: Sequence[str]) -> str:
    joined = "\n".join(sorted(set(report_ids)))
    return hashlib.sha256(joined.encode()).hexdigest()


def check_confirmable(
    proposal: DeletionProposal,
    current: Mapping[str, CurrentReport],
    now: datetime,
) -> None:
    """Raise unless confirming ``proposal`` now deletes exactly its reports.

    Called with the proposal and its reports locked, so the answer cannot
    change before the deletion is applied.
    """
    if proposal.status is not ProposalStatus.PENDING:
        raise DeletionError(
            DeletionErrorCode.ALREADY_RESOLVED,
            "this proposal was already confirmed or cancelled",
        )
    if proposal.expired_at(now):
        raise DeletionError(
            DeletionErrorCode.EXPIRED,
            "this proposal expired; ask for the deletion again",
        )
    for item in proposal.items:
        report = current.get(item.report_id)
        if (
            report is None
            or report.owner_id != proposal.owner_id
            or report.deleted
            or report.latest_version != item.version
        ):
            raise DeletionError(
                DeletionErrorCode.STALE,
                "the reports changed since this was proposed; nothing was deleted. "
                "Ask for the deletion again.",
            )
