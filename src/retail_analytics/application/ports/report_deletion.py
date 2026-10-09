from __future__ import annotations

from datetime import datetime
from typing import Protocol

from retail_analytics.application.contracts.report_deletion import NewDeletionProposal
from retail_analytics.domain.report_deletion import DeletionProposal


class ReportDeletionRepository(Protocol):
    """Proposals and the atomic deletion they authorize (PostgreSQL).

    Every call is scoped to one owner; another owner's proposal or report is
    indistinguishable from a missing one (``AccessDenied``).
    """

    async def propose(self, new: NewDeletionProposal) -> tuple[DeletionProposal, bool]:
        """Freeze the live reports ``new.report_ids`` at their current versions.

        An existing ``(owner, idempotency_key)`` returns the original with
        ``False`` (``DeletionError`` IDEMPOTENCY_CONFLICT if it named other
        reports). Raises ``AccessDenied`` for a report that is not the
        owner's live report and ``DeletionError`` TOO_MANY_PENDING.
        """
        ...

    async def get(self, owner_id: str, proposal_id: str) -> DeletionProposal:
        """Raises ``AccessDenied`` when it is not this owner's."""
        ...

    async def list_pending(
        self, owner_id: str, *, at: datetime, limit: int
    ) -> tuple[DeletionProposal, ...]:
        """This owner's proposals that are pending and not yet expired at ``at``,
        newest first."""
        ...

    async def confirm(
        self, owner_id: str, proposal_id: str, *, at: datetime, audit_id: str
    ) -> DeletionProposal:
        """In one transaction: lock the proposal and its reports, recheck owner,
        expiry, status and versions, soft-delete every proposed report, consume
        the proposal and append the audit event. Any failure, including the
        audit insert, changes nothing. Raises ``DeletionError`` or
        ``AccessDenied``.
        """
        ...

    async def cancel(
        self, owner_id: str, proposal_id: str, *, at: datetime, audit_id: str
    ) -> DeletionProposal:
        """Consume a pending proposal without deleting anything."""
        ...
