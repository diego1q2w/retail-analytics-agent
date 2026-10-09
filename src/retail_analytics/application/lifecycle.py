"""Manual report recovery and bounded lifecycle cleanup.

Recovery
--------
``LifecycleService.restore`` is the only way to bring back a soft-deleted
report. It is an operator or authenticated-user operation: nothing registers
it as a model capability, and the model cannot reach it through another tool.
The owner (with the delete permission) or an access administrator may restore,
only strictly before ``deleted_at + RECOVERY_PERIOD``, only if the report has
not been purged, and only while the owner is still an active executive. The
store rechecks all of it under the report's row lock, so a restore and a purge
of the same report have exactly one winner. Restoring changes only
``deleted_at``: the consumed deletion confirmation stays consumed.

Cleanup
-------
``run_maintenance`` is idempotent, crash-safe and bounded per run:

1. Purge reports past their recovery period. Phase one removes versions,
   citations and evidence pins and leaves a content-free tombstone; then
   ``ArtifactMaintenance.purge`` deletes the artifact (metadata, then bytes);
   phase three deletes the tombstone and appends the audit event. An
   interruption anywhere leaves a tombstone that the next run finishes.
   Reports whose runs still have unresolved operations are skipped.
2. Remove expired investigations: seven days after the later of last
   interaction and last run completion. Evidence pinned or cited by a saved
   report (and what it derives from) survives, as do the records of
   unresolved external operations.
3. Flag long-unresolved operations for manual resolution (audit event).
4. Delete audit events past the configured retention.
5. Reconcile artifact storage.

A dry run reports counts only; it reads nothing it could reveal and changes
nothing.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from retail_analytics.application.artifacts import ArtifactMaintenance
from retail_analytics.application.authorization import AccessDenied, AccessResolver
from retail_analytics.application.contracts.authorization import Principal
from retail_analytics.application.contracts.lifecycle import (
    ContentRemoval,
    InvestigationCleanup,
    MaintenanceLimits,
    MaintenanceReport,
    RestorableReport,
    RestoredReport,
    UnresolvedOperation,
)
from retail_analytics.application.ports.lifecycle import LifecycleStore
from retail_analytics.domain.access import Permission
from retail_analytics.domain.lifecycle import RetentionPolicy

type Clock = Callable[[], datetime]
type IdFactory = Callable[[], str]

_PAGE = 100


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return uuid.uuid4().hex


class LifecycleService:
    def __init__(
        self,
        store: LifecycleStore,
        resolver: AccessResolver,
        artifacts: ArtifactMaintenance,
        *,
        policy: RetentionPolicy | None = None,
        clock: Clock = _utc_now,
        new_id: IdFactory = _new_id,
    ) -> None:
        self._store = store
        self._resolver = resolver
        self._artifacts = artifacts
        self._policy = policy or RetentionPolicy()
        self._clock = clock
        self._new_id = new_id

    # --- manual recovery ------------------------------------------------------

    async def restore(self, principal: Principal, report_id: str) -> RestoredReport:
        """Restore one soft-deleted report; raises ``AccessDenied`` or
        ``RestoreError``. Never call this from model output."""
        permissions = await self._resolver.effective_permissions(principal)
        is_admin = Permission.ACCESS_ADMIN.value in permissions
        if not is_admin and Permission.REPORTS_DELETE_OWN.value not in permissions:
            raise AccessDenied("report", report_id)
        return await self._store.restore_report(
            report_id=report_id,
            actor_id=principal.executive_id,
            actor_is_admin=is_admin,
            at=self._clock(),
            audit_id=self._new_id(),
        )

    async def list_restorable(
        self, principal: Principal
    ) -> tuple[RestorableReport, ...]:
        """The caller's own reports still inside the recovery period."""
        await self._resolver.require_permission(
            principal, Permission.REPORTS_DELETE_OWN
        )
        found = await self._store.list_restorable(principal.executive_id, self._clock())
        return tuple(found)

    async def unresolved_operations(
        self, limit: int = 100
    ) -> tuple[UnresolvedOperation, ...]:
        """Operations awaiting manual resolution (trusted operator view)."""
        return tuple(await self._store.unresolved_operations(limit))

    # --- cleanup --------------------------------------------------------------

    async def run_maintenance(
        self,
        limits: MaintenanceLimits | None = None,
        *,
        dry_run: bool = False,
    ) -> MaintenanceReport:
        limits = limits or MaintenanceLimits(max_reports=self._policy.batch_size)
        now = self._clock()
        audit_cutoff = now - self._policy.audit_retention
        if dry_run:
            counts = await self._store.preview(
                now,
                audit_cutoff=audit_cutoff,
                flag_after=self._policy.unresolved_flag_after,
            )
            return MaintenanceReport(
                dry_run=True,
                reports_purged=counts.reports_due,
                investigations=InvestigationCleanup(
                    sessions_removed=counts.investigations_expired
                ),
                audit_rows_removed=counts.audit_rows_due,
                unresolved_flagged=counts.unresolved_to_flag,
            )

        purged = blocked = failed = 0
        due = await self._store.due_reports(now, limits.max_reports + 1)
        more = len(due) > limits.max_reports
        for report_id in due[: limits.max_reports]:
            try:
                outcome = await self._purge_report(report_id, now)
            except Exception:  # one bad report must not stop the run
                failed += 1
                continue
            if outcome is True:
                purged += 1
            elif outcome is ContentRemoval.BLOCKED:
                blocked += 1

        investigations = await self._store.purge_expired_investigations(
            now, limit=limits.max_sessions
        )
        more = more or investigations.sessions_scanned >= limits.max_sessions
        flagged = await self._store.flag_unresolved(
            now - self._policy.unresolved_flag_after, now, limit=_PAGE
        )
        audit_removed = await self._store.delete_audit_before(
            audit_cutoff, limits.max_audit_rows
        )
        more = more or audit_removed >= limits.max_audit_rows
        reconcile = await self._artifacts.reconcile()
        return MaintenanceReport(
            dry_run=False,
            reports_purged=purged,
            reports_blocked=blocked,
            reports_failed=failed,
            investigations=investigations,
            audit_rows_removed=audit_removed,
            unresolved_flagged=flagged,
            artifact_partials_removed=reconcile.partials_removed,
            artifact_orphans_removed=reconcile.orphans_removed,
            artifact_missing_content=len(reconcile.missing_content),
            more_pending=more,
        )

    async def _purge_report(
        self, report_id: str, now: datetime
    ) -> bool | ContentRemoval:
        removal = await self._store.remove_report_content(report_id, now)
        if removal is not ContentRemoval.REMOVED:
            return removal
        # The artifact id is the report id. Metadata goes before bytes, and a
        # failure leaves the tombstone for the next run.
        await self._artifacts.purge(report_id)
        return await self._store.finish_report_purge(report_id, now, self._new_id())
