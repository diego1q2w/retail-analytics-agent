from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Protocol

from retail_analytics.application.contracts.lifecycle import (
    ContentRemoval,
    InvestigationCleanup,
    PreviewCounts,
    RestorableReport,
    RestoredReport,
    UnresolvedOperation,
)


class LifecycleStore(Protocol):
    """Transactional restore and cleanup over PostgreSQL.

    Each method is one transaction that takes the row locks it needs and
    rechecks its preconditions under them, so competing callers have exactly
    one winner and a repeat or interrupted call is harmless.
    """

    async def restore_report(
        self,
        *,
        report_id: str,
        actor_id: str,
        actor_is_admin: bool,
        at: datetime,
        audit_id: str,
    ) -> RestoredReport:
        """Un-delete before the deadline; raises ``RestoreError`` otherwise.

        Unknown and not-permitted reports raise the same error. The owner must
        still be an active executive. The audit event commits with the change.
        """
        ...

    async def list_restorable(
        self, owner_id: str, at: datetime
    ) -> Sequence[RestorableReport]: ...

    async def due_reports(self, at: datetime, limit: int) -> Sequence[str]:
        """Soft-deleted reports whose recovery period has ended, oldest first."""
        ...

    async def remove_report_content(
        self, report_id: str, at: datetime
    ) -> ContentRemoval:
        """Drop a due report's versions, citations, evidence pins and proposal
        items, leaving a content-free tombstone row until its artifact is gone."""
        ...

    async def finish_report_purge(
        self, report_id: str, at: datetime, audit_id: str
    ) -> bool:
        """Delete the tombstone and append the audit event, if its artifact is
        gone. False means nothing was finished (retry later)."""
        ...

    async def purge_expired_investigations(
        self, at: datetime, *, limit: int
    ) -> InvestigationCleanup:
        """Clean up to ``limit`` expired investigations (see ``domain.lifecycle``)."""
        ...

    async def delete_audit_before(self, cutoff: datetime, limit: int) -> int: ...

    async def flag_unresolved(
        self, older_than: datetime, at: datetime, *, limit: int
    ) -> int:
        """Append one audit flag per long-unresolved operation, once each."""
        ...

    async def unresolved_operations(
        self, limit: int
    ) -> Sequence[UnresolvedOperation]: ...

    async def preview(
        self, at: datetime, *, audit_cutoff: datetime, flag_after: timedelta
    ) -> PreviewCounts: ...
