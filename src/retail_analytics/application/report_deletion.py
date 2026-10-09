"""Exact deletion of saved reports: the model proposes, the application confirms.

Flow
----
1. ``propose`` (the model-facing capability calls this) freezes the exact
   reports, by ID and current version, that the requester owns. It names IDs
   the model resolved with ``ReportService.search``/``list_reports`` (both
   owner-only); nothing is searched again afterwards. The proposal expires
   after ten minutes and deletes nothing.
2. The application shows ``DeletionPreview`` (titles, dates, count, expiry) to
   the user. Only the authenticated user's explicit confirmation reaches
   ``confirm``. There is no model tool for it and the capability's arguments
   cannot carry approval; a "yes" the model writes is just text.
3. ``confirm`` runs one transaction: it locks the proposal and its reports,
   rechecks requester, status, expiry, ownership and versions, soft-deletes
   all of them, consumes the proposal and appends the audit event. A failure
   at any point, including the audit insert, changes nothing. Confirmation by
   another principal fails like an unknown proposal. A replay or a concurrent
   second confirmation finds the proposal consumed and fails.

Soft-deleted reports disappear from list, search, read and export at once and
stay recoverable by an operator for ``RECOVERY_PERIOD`` (seven days). Purging
is a separate maintenance step; there is no agent tool to restore.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from retail_analytics.application.authorization import AccessDenied, AccessResolver
from retail_analytics.application.contracts.authorization import Principal
from retail_analytics.application.contracts.report_deletion import (
    DeletionPreview,
    DeletionResult,
    NewDeletionProposal,
    PreviewItem,
)
from retail_analytics.application.contracts.tools import OperationContext
from retail_analytics.application.ports.report_deletion import ReportDeletionRepository
from retail_analytics.domain.access import Permission, ProductScope
from retail_analytics.domain.evidence import scope_digest
from retail_analytics.domain.report_deletion import (
    PROPOSAL_TTL,
    RECOVERY_PERIOD,
    DeletionError,
    DeletionErrorCode,
    DeletionProposal,
    ProposalStatus,
    clean_report_ids,
)

type Clock = Callable[[], datetime]
type IdFactory = Callable[[], str]

_TITLE_CHARS = 200


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return uuid.uuid4().hex


def display_title(title: str) -> str:
    """A title is data written by a model, so show it flattened and bounded."""
    flat = "".join(c if c.isprintable() else " " for c in title)
    return " ".join(flat.split())[:_TITLE_CHARS]


class ReportDeletionService:
    def __init__(
        self,
        repository: ReportDeletionRepository,
        resolver: AccessResolver,
        *,
        clock: Clock = _utc_now,
        new_id: IdFactory = _new_id,
    ) -> None:
        self._repository = repository
        self._resolver = resolver
        self._clock = clock
        self._new_id = new_id

    async def propose(
        self, ctx: OperationContext, report_ids: tuple[str, ...]
    ) -> DeletionPreview:
        """Freeze ``report_ids`` as a pending proposal; deletes nothing.

        ``ctx`` is the trusted per-attempt context; ``ctx.operation_id`` is the
        idempotency key, so a retried call returns the same proposal. Raises
        ``AccessDenied`` (no delete permission, or a report that is not the
        requester's live report) or ``DeletionError``.
        """
        execution = ctx.execution
        if Permission.REPORTS_DELETE_OWN.value not in execution.permissions:
            raise AccessDenied("permission", Permission.REPORTS_DELETE_OWN.value)
        ids = clean_report_ids(report_ids)
        now = self._clock()
        proposal, created = await self._repository.propose(
            NewDeletionProposal(
                proposal_id=self._new_id(),
                audit_id=self._new_id(),
                owner_id=execution.executive_id,
                session_id=execution.correlation.session_id,
                run_id=execution.correlation.run_id,
                report_ids=ids,
                idempotency_key=ctx.operation_id,
                created_at=now,
                expires_at=now + PROPOSAL_TTL,
            )
        )
        return _preview(proposal, execution.product_scope, duplicate=not created)

    async def preview(self, principal: Principal, proposal_id: str) -> DeletionPreview:
        scope = await self._scope(principal)
        proposal = await self._repository.get(principal.executive_id, proposal_id)
        return _preview(proposal, scope)

    async def confirm(self, principal: Principal, proposal_id: str) -> DeletionResult:
        """Delete exactly the proposed reports, once.

        Call only from an authenticated user's explicit confirmation (the CLI
        ``confirm <proposal-id>``), never from model output. Raises
        ``AccessDenied`` (unknown or another principal's proposal, or no
        permission) or ``DeletionError`` (EXPIRED, ALREADY_RESOLVED, STALE);
        in every failure nothing was deleted.
        """
        await self._scope(principal)
        now = self._clock()
        proposal = await self._repository.confirm(
            principal.executive_id,
            proposal_id,
            at=now,
            audit_id=self._new_id(),
        )
        return DeletionResult(
            proposal_id=proposal.proposal_id,
            report_ids=tuple(i.report_id for i in proposal.items),
            deleted_at=now,
            recoverable_until=now + RECOVERY_PERIOD,
        )

    async def cancel(self, principal: Principal, proposal_id: str) -> DeletionPreview:
        """Withdraw a pending proposal; nothing is deleted."""
        scope = await self._scope(principal)
        proposal = await self._repository.cancel(
            principal.executive_id,
            proposal_id,
            at=self._clock(),
            audit_id=self._new_id(),
        )
        return _preview(proposal, scope)

    async def _scope(self, principal: Principal) -> ProductScope:
        access = await self._resolver.require_permission(
            principal, Permission.REPORTS_DELETE_OWN
        )
        return access.product_scope


def _preview(
    proposal: DeletionProposal, scope: ProductScope, *, duplicate: bool = False
) -> DeletionPreview:
    current = None if scope.is_empty else scope_digest(scope)
    return DeletionPreview(
        proposal_id=proposal.proposal_id,
        status=proposal.status,
        expires_at=proposal.expires_at,
        items=tuple(
            PreviewItem(
                report_id=i.report_id,
                version=i.version,
                title=(display_title(i.title) if i.scope_digest == current else None),
                created_at=i.created_at,
            )
            for i in proposal.items
        ),
        duplicate=duplicate,
    )


__all__ = [
    "DeletionError",
    "DeletionErrorCode",
    "ProposalStatus",
    "ReportDeletionService",
]
