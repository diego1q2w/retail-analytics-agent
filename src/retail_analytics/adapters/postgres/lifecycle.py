"""PostgreSQL ``LifecycleStore``: report restore/purge and investigation cleanup.

Lock order, the same everywhere here: the report row, or the session row and
then its evidence rows in sorted ID order. Restore and report purge both lock
the ``reports`` row and recheck the report's state under it, so one of them
wins and the other sees the result. Investigation cleanup locks the evidence it
may delete and only then reads the pins and citations, in a fresh statement, so
a pin committed before the lock is seen and a pin attempted after it fails on
the foreign key instead of pointing at deleted evidence.

Nothing here selects report titles or text; audit details are identifiers and
counts only.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa

from retail_analytics.adapters.postgres.audit import append_audit
from retail_analytics.adapters.postgres.database import Database
from retail_analytics.adapters.postgres.schema import artifact_versions as av
from retail_analytics.adapters.postgres.schema import audit_events as au
from retail_analytics.adapters.postgres.schema import budget_charges as bc
from retail_analytics.adapters.postgres.schema import deletion_proposal_items as di
from retail_analytics.adapters.postgres.schema import evidence as ev
from retail_analytics.adapters.postgres.schema import evidence_dependencies as ed
from retail_analytics.adapters.postgres.schema import evidence_pins as ep
from retail_analytics.adapters.postgres.schema import execution_events as xe
from retail_analytics.adapters.postgres.schema import executives as ex
from retail_analytics.adapters.postgres.schema import messages as ms
from retail_analytics.adapters.postgres.schema import query_executions as qe
from retail_analytics.adapters.postgres.schema import report_evidence as re_
from retail_analytics.adapters.postgres.schema import report_versions as rv
from retail_analytics.adapters.postgres.schema import reports as rp
from retail_analytics.adapters.postgres.schema import run_budgets as rb
from retail_analytics.adapters.postgres.schema import run_events as rx
from retail_analytics.adapters.postgres.schema import run_inputs as ri
from retail_analytics.adapters.postgres.schema import run_principals as rpr
from retail_analytics.adapters.postgres.schema import run_questions as rq
from retail_analytics.adapters.postgres.schema import runs as rn
from retail_analytics.adapters.postgres.schema import sessions as ss
from retail_analytics.adapters.postgres.schema import tool_executions as te
from retail_analytics.application.contracts.audit import AuditEvent
from retail_analytics.application.contracts.lifecycle import (
    ContentRemoval,
    InvestigationCleanup,
    PreviewCounts,
    RestorableReport,
    RestoredReport,
    UnresolvedOperation,
)
from retail_analytics.domain.lifecycle import (
    INVESTIGATION_RETENTION,
    PURGED,
    RESTORED,
    SYSTEM_ACTOR,
    UNRESOLVED_FLAGGED,
    RestoreError,
    RestoreErrorCode,
    investigation_expires_at,
    is_purgeable,
    is_restorable,
    recoverable_until,
    removable_evidence,
)
from retail_analytics.domain.report_deletion import RECOVERY_PERIOD

# Scan at most this many expired sessions per requested cleanup, so sessions
# kept as shells (pinned evidence) cannot starve the ones that need work.
_SCAN_FACTOR = 10
_ACTIVE_RUNS = ("running", "waiting_for_input", "cancelling")
_IN_FLIGHT_OPERATIONS = (
    "submitting",
    "running",
    "outcome_unknown",
    "retrying",
    "cancel_requested",
)
_EXTERNAL_EFFECTS = ("external_job", "external_delivery")
_FLAG_PREFIX = "unresolved:"

type Connection = sa.Connection


def _unresolved() -> sa.ColumnElement[bool]:
    """Operations that may have an effect or job outside our records.

    ``prepared`` has sent nothing yet. Once submitted, an external operation
    keeps its records until it ends; an unknown outcome always does.
    """
    return sa.and_(
        te.c.status.in_(_IN_FLIGHT_OPERATIONS),
        sa.or_(
            te.c.side_effect.in_(_EXTERNAL_EFFECTS),
            te.c.status == "outcome_unknown",
        ),
    )


def _expiry_candidates(cutoff: datetime) -> sa.Select[Any]:
    """Sessions whose latest signal is older than the retention cutoff."""
    completed = (
        sa.select(sa.func.max(rn.c.completed_at))
        .where(rn.c.session_id == ss.c.session_id)
        .scalar_subquery()
    )
    return sa.select(ss.c.session_id, ss.c.last_activity_at).where(
        sa.func.greatest(
            ss.c.last_activity_at, sa.func.coalesce(completed, ss.c.last_activity_at)
        )
        <= cutoff
    )


def _lock(connection: Connection, key: str) -> None:
    connection.execute(
        sa.select(sa.func.pg_advisory_xact_lock(sa.func.hashtextextended(key, 0)))
    )


class PostgresLifecycleStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    # --- restore ---------------------------------------------------------------

    async def restore_report(
        self,
        *,
        report_id: str,
        actor_id: str,
        actor_is_admin: bool,
        at: datetime,
        audit_id: str,
    ) -> RestoredReport:
        return await self._db.transaction(
            self._restore, report_id, actor_id, actor_is_admin, at, audit_id
        )

    @staticmethod
    def _restore(
        connection: Connection,
        report_id: str,
        actor_id: str,
        actor_is_admin: bool,
        at: datetime,
        audit_id: str,
    ) -> RestoredReport:
        row = connection.execute(
            sa.select(rp.c.owner_id, rp.c.deleted_at)
            .where(rp.c.report_id == report_id)
            .with_for_update()
        ).one_or_none()
        if row is None or not (actor_is_admin or row.owner_id == actor_id):
            raise RestoreError(RestoreErrorCode.NOT_FOUND, "no such report")
        if row.deleted_at is None:
            raise RestoreError(
                RestoreErrorCode.NOT_DELETED, "the report is not deleted"
            )
        versions = connection.execute(
            sa.select(sa.func.count())
            .select_from(rv)
            .where(rv.c.report_id == report_id)
        ).scalar_one()
        if versions == 0:
            raise RestoreError(
                RestoreErrorCode.PURGED, "the report was permanently removed"
            )
        if not is_restorable(row.deleted_at, at):
            raise RestoreError(
                RestoreErrorCode.WINDOW_CLOSED,
                "the recovery period for this report has ended",
            )
        owner_active = connection.execute(
            sa.select(ex.c.active).where(ex.c.executive_id == row.owner_id)
        ).scalar_one_or_none()
        if not owner_active:
            raise RestoreError(
                RestoreErrorCode.OWNER_UNAVAILABLE,
                "the report's owner is no longer an active executive",
            )
        connection.execute(
            sa.update(rp).where(rp.c.report_id == report_id).values(deleted_at=None)
        )
        append_audit(
            connection,
            AuditEvent(
                audit_id=audit_id,
                occurred_at=at,
                actor_id=actor_id,
                action=RESTORED,
                subject_type="report",
                subject_id=report_id,
                details={
                    "owner_id": row.owner_id,
                    "deleted_at": row.deleted_at.isoformat(),
                    "by_admin": actor_is_admin and row.owner_id != actor_id,
                },
            ),
        )
        return RestoredReport(report_id, row.owner_id, at)

    async def list_restorable(
        self, owner_id: str, at: datetime
    ) -> Sequence[RestorableReport]:
        def work(connection: Connection) -> list[RestorableReport]:
            rows = connection.execute(
                sa.select(rp.c.report_id, rp.c.created_at, rp.c.deleted_at)
                .where(
                    rp.c.owner_id == owner_id,
                    rp.c.deleted_at.is_not(None),
                    rp.c.deleted_at > at - RECOVERY_PERIOD,
                    sa.exists().where(rv.c.report_id == rp.c.report_id),
                )
                .order_by(rp.c.deleted_at.desc(), rp.c.report_id)
            )
            return [
                RestorableReport(
                    r.report_id,
                    r.created_at,
                    r.deleted_at,
                    recoverable_until(r.deleted_at),
                )
                for r in rows
            ]

        return await self._db.transaction(work)

    # --- report purge ------------------------------------------------------------

    async def due_reports(self, at: datetime, limit: int) -> Sequence[str]:
        def work(connection: Connection) -> list[str]:
            return list(
                connection.execute(
                    sa.select(rp.c.report_id)
                    .where(
                        rp.c.deleted_at.is_not(None),
                        rp.c.deleted_at <= at - RECOVERY_PERIOD,
                    )
                    .order_by(rp.c.deleted_at, rp.c.report_id)
                    .limit(limit)
                ).scalars()
            )

        return await self._db.transaction(work)

    async def remove_report_content(
        self, report_id: str, at: datetime
    ) -> ContentRemoval:
        return await self._db.transaction(self._remove_content, report_id, at)

    @staticmethod
    def _remove_content(
        connection: Connection, report_id: str, at: datetime
    ) -> ContentRemoval:
        row = connection.execute(
            sa.select(rp.c.deleted_at)
            .where(rp.c.report_id == report_id)
            .with_for_update()
        ).one_or_none()
        if (
            row is None
            or row.deleted_at is None
            or not is_purgeable(row.deleted_at, at)
        ):
            return ContentRemoval.SKIPPED
        has_versions = connection.execute(
            sa.select(sa.exists().where(rv.c.report_id == report_id))
        ).scalar_one()
        if not has_versions:
            return ContentRemoval.REMOVED  # an interrupted purge: finish it

        runs = {
            r
            for r in connection.execute(
                sa.select(rv.c.run_id).where(rv.c.report_id == report_id)
            ).scalars()
            if r is not None
        }
        runs |= set(
            connection.execute(
                sa.select(ev.c.run_id)
                .select_from(re_.join(ev, ev.c.evidence_id == re_.c.evidence_id))
                .where(re_.c.report_id == report_id)
            ).scalars()
        )
        if (
            runs
            and connection.execute(
                sa.select(sa.exists().where(te.c.run_id.in_(runs), _unresolved()))
            ).scalar_one()
        ):
            return ContentRemoval.BLOCKED

        connection.execute(sa.delete(di).where(di.c.report_id == report_id))
        connection.execute(sa.delete(re_).where(re_.c.report_id == report_id))
        connection.execute(
            sa.delete(ep).where(
                ep.c.holder_kind == "report", ep.c.holder_id == report_id
            )
        )
        connection.execute(sa.delete(rv).where(rv.c.report_id == report_id))
        return ContentRemoval.REMOVED

    async def finish_report_purge(
        self, report_id: str, at: datetime, audit_id: str
    ) -> bool:
        return await self._db.transaction(self._finish_purge, report_id, at, audit_id)

    @staticmethod
    def _finish_purge(
        connection: Connection, report_id: str, at: datetime, audit_id: str
    ) -> bool:
        row = connection.execute(
            sa.select(rp.c.owner_id, rp.c.deleted_at)
            .where(rp.c.report_id == report_id)
            .with_for_update()
        ).one_or_none()
        if row is None or row.deleted_at is None:
            return False
        remaining = connection.execute(
            sa.select(
                sa.exists().where(rv.c.report_id == report_id)
                | sa.exists().where(av.c.artifact_id == report_id)
            )
        ).scalar_one()
        if remaining:
            return False
        connection.execute(sa.delete(rp).where(rp.c.report_id == report_id))
        append_audit(
            connection,
            AuditEvent(
                audit_id=audit_id,
                occurred_at=at,
                actor_id=SYSTEM_ACTOR,
                action=PURGED,
                subject_type="report",
                subject_id=report_id,
                details={
                    "owner_id": row.owner_id,
                    "deleted_at": row.deleted_at.isoformat(),
                },
            ),
        )
        return True

    # --- investigations ----------------------------------------------------------

    async def purge_expired_investigations(
        self, at: datetime, *, limit: int
    ) -> InvestigationCleanup:
        cutoff = at - INVESTIGATION_RETENTION
        total = InvestigationCleanup()
        worked = 0
        after: tuple[datetime, str] | None = None
        scan_cap = limit * _SCAN_FACTOR
        while worked < limit and total.sessions_scanned < scan_cap:
            page = await self._db.transaction(self._page, cutoff, after, limit)
            if not page:
                break
            after = page[-1]
            for _, session_id in page:
                if worked >= limit or total.sessions_scanned >= scan_cap:
                    break
                result, did_work = await self._db.transaction(
                    self._purge_session, session_id, at
                )
                total += result
                worked += 1 if did_work else 0
        return total

    @staticmethod
    def _page(
        connection: Connection,
        cutoff: datetime,
        after: tuple[datetime, str] | None,
        size: int,
    ) -> list[tuple[datetime, str]]:
        query = _expiry_candidates(cutoff)
        if after is not None:
            query = query.where(
                sa.tuple_(ss.c.last_activity_at, ss.c.session_id) > sa.tuple_(*after)
            )
        rows = connection.execute(
            query.order_by(ss.c.last_activity_at, ss.c.session_id).limit(size)
        )
        return [(r.last_activity_at, r.session_id) for r in rows]

    @staticmethod
    def _purge_session(
        connection: Connection, session_id: str, at: datetime
    ) -> tuple[InvestigationCleanup, bool]:
        scanned = InvestigationCleanup(sessions_scanned=1)
        session = connection.execute(
            sa.select(ss.c.last_activity_at)
            .where(ss.c.session_id == session_id)
            .with_for_update()
        ).one_or_none()
        if session is None:
            return InvestigationCleanup(), False
        last_run = connection.execute(
            sa.select(sa.func.max(rn.c.completed_at)).where(
                rn.c.session_id == session_id
            )
        ).scalar_one()
        if investigation_expires_at(session.last_activity_at, last_run) > at:
            return InvestigationCleanup(), False

        run_ids = list(
            connection.execute(
                sa.select(rn.c.run_id).where(rn.c.session_id == session_id)
            ).scalars()
        )
        if connection.execute(
            sa.select(
                sa.exists().where(
                    rn.c.session_id == session_id, rn.c.status.in_(_ACTIVE_RUNS)
                )
            )
        ).scalar_one():
            return scanned + InvestigationCleanup(sessions_held_active=1), False
        if (
            run_ids
            and connection.execute(
                sa.select(sa.exists().where(te.c.run_id.in_(run_ids), _unresolved()))
            ).scalar_one()
        ):
            return scanned + InvestigationCleanup(sessions_held_unresolved=1), False

        # Lock the session's evidence first; pins and citations are read after.
        evidence_ids = list(
            connection.execute(
                sa.select(ev.c.evidence_id)
                .where(ev.c.session_id == session_id)
                .order_by(ev.c.evidence_id)
                .with_for_update()
            ).scalars()
        )
        removable = _removable(connection, evidence_ids)
        did_work = False
        if removable:
            connection.execute(sa.delete(ev).where(ev.c.evidence_id.in_(removable)))
            did_work = True
        kept = [e for e in evidence_ids if e not in removable]

        kept_runs: set[str] = set()
        kept_ops: set[str] = set()
        if kept:
            for r in connection.execute(
                sa.select(ev.c.run_id, ev.c.operation_id).where(
                    ev.c.evidence_id.in_(kept)
                )
            ):
                kept_runs.add(r.run_id)
                kept_ops.add(r.operation_id)

        did_work |= _clear_run_records(
            connection, session_id, run_ids, kept_runs, kept_ops
        )
        if kept_runs:
            return (
                scanned
                + InvestigationCleanup(
                    sessions_shell_kept=1, evidence_removed=len(removable)
                ),
                did_work,
            )
        connection.execute(sa.delete(ss).where(ss.c.session_id == session_id))
        return (
            scanned
            + InvestigationCleanup(sessions_removed=1, evidence_removed=len(removable)),
            True,
        )

    # --- audit and unresolved operations -----------------------------------------

    async def delete_audit_before(self, cutoff: datetime, limit: int) -> int:
        def work(connection: Connection) -> int:
            oldest = (
                sa.select(au.c.audit_id)
                .where(au.c.occurred_at < cutoff)
                .order_by(au.c.occurred_at, au.c.audit_id)
                .limit(limit)
            )
            return connection.execute(
                sa.delete(au).where(au.c.audit_id.in_(oldest))
            ).rowcount

        return await self._db.transaction(work)

    async def flag_unresolved(
        self, older_than: datetime, at: datetime, *, limit: int
    ) -> int:
        return await self._db.transaction(self._flag, older_than, at, limit)

    @staticmethod
    def _flag(
        connection: Connection, older_than: datetime, at: datetime, limit: int
    ) -> int:
        _lock(connection, "lifecycle:flag-unresolved")
        flagged = (
            sa.select(sa.literal(1))
            .where(au.c.audit_id == sa.literal(_FLAG_PREFIX) + te.c.operation_id)
            .exists()
        )
        rows = connection.execute(
            sa.select(
                te.c.operation_id,
                te.c.run_id,
                te.c.capability,
                te.c.status,
                te.c.updated_at,
            )
            .where(_unresolved(), te.c.updated_at < older_than, ~flagged)
            .order_by(te.c.updated_at, te.c.operation_id)
            .limit(limit)
        ).all()
        for r in rows:
            append_audit(
                connection,
                AuditEvent(
                    audit_id=f"{_FLAG_PREFIX}{r.operation_id}",
                    occurred_at=at,
                    actor_id=SYSTEM_ACTOR,
                    action=UNRESOLVED_FLAGGED,
                    subject_type="operation",
                    subject_id=r.operation_id,
                    run_id=r.run_id,
                    details={
                        "capability": r.capability,
                        "status": r.status,
                        "since": r.updated_at.isoformat(),
                    },
                ),
            )
        return len(rows)

    async def unresolved_operations(self, limit: int) -> Sequence[UnresolvedOperation]:
        def work(connection: Connection) -> list[UnresolvedOperation]:
            rows = connection.execute(
                sa.select(
                    te.c.operation_id,
                    te.c.run_id,
                    te.c.capability,
                    te.c.status,
                    te.c.updated_at,
                )
                .where(
                    _unresolved(),
                    sa.exists().where(
                        au.c.audit_id == sa.literal(_FLAG_PREFIX) + te.c.operation_id
                    ),
                )
                .order_by(te.c.updated_at, te.c.operation_id)
                .limit(limit)
            )
            return [
                UnresolvedOperation(
                    r.operation_id, r.run_id, r.capability, r.status, r.updated_at
                )
                for r in rows
            ]

        return await self._db.transaction(work)

    # --- dry run -------------------------------------------------------------------

    async def preview(
        self, at: datetime, *, audit_cutoff: datetime, flag_after: timedelta
    ) -> PreviewCounts:
        def work(connection: Connection) -> PreviewCounts:
            due = connection.execute(
                sa.select(sa.func.count()).where(
                    rp.c.deleted_at.is_not(None),
                    rp.c.deleted_at <= at - RECOVERY_PERIOD,
                )
            ).scalar_one()
            expired = connection.execute(
                sa.select(sa.func.count()).select_from(
                    _expiry_candidates(at - INVESTIGATION_RETENTION).subquery()
                )
            ).scalar_one()
            old_audit = connection.execute(
                sa.select(sa.func.count()).where(au.c.occurred_at < audit_cutoff)
            ).scalar_one()
            unflagged = connection.execute(
                sa.select(sa.func.count())
                .select_from(te)
                .where(
                    _unresolved(),
                    te.c.updated_at < at - flag_after,
                    ~sa.exists().where(
                        au.c.audit_id == sa.literal(_FLAG_PREFIX) + te.c.operation_id
                    ),
                )
            ).scalar_one()
            return PreviewCounts(due, expired, old_audit, unflagged)

        return await self._db.transaction(work)


def _removable(connection: Connection, evidence_ids: list[str]) -> frozenset[str]:
    if not evidence_ids:
        return frozenset()
    pinned = set(
        connection.execute(
            sa.select(ep.c.evidence_id).where(ep.c.evidence_id.in_(evidence_ids))
        ).scalars()
    )
    cited = set(
        connection.execute(
            sa.select(re_.c.evidence_id).where(re_.c.evidence_id.in_(evidence_ids))
        ).scalars()
    )
    inside = set(evidence_ids)
    dependencies: dict[str, list[str]] = {}
    external: set[str] = set()
    for r in connection.execute(
        sa.select(ed.c.evidence_id, ed.c.depends_on).where(
            sa.or_(
                ed.c.evidence_id.in_(evidence_ids), ed.c.depends_on.in_(evidence_ids)
            )
        )
    ):
        dependencies.setdefault(r.evidence_id, []).append(r.depends_on)
        if r.evidence_id not in inside and r.depends_on in inside:
            external.add(r.depends_on)
    return removable_evidence(
        evidence_ids,
        pinned=pinned,
        cited=cited,
        dependencies=dependencies,
        external_dependents=external,
    )


def _clear_run_records(
    connection: Connection,
    session_id: str,
    run_ids: list[str],
    kept_runs: set[str],
    kept_ops: set[str],
) -> bool:
    """Delete messages, progress and run bookkeeping of an expired session.

    Runs and operations that retained evidence points at stay as bare rows.
    Returns whether anything was deleted.
    """
    deleted = 0

    def run(statement: sa.Executable) -> None:
        nonlocal deleted
        deleted += max(connection.execute(statement).rowcount, 0)

    ops = (
        list(
            connection.execute(
                sa.select(te.c.operation_id).where(te.c.run_id.in_(run_ids))
            ).scalars()
        )
        if run_ids
        else []
    )
    drop_ops = [o for o in ops if o not in kept_ops]
    dead_runs = [r for r in run_ids if r not in kept_runs]

    if ops:
        run(sa.delete(xe).where(xe.c.operation_id.in_(ops)))
    if drop_ops:
        run(sa.delete(qe).where(qe.c.operation_id.in_(drop_ops)))
        run(sa.delete(te).where(te.c.operation_id.in_(drop_ops)))
    if run_ids:
        for table in (rx, rq, rb, bc, rpr):
            run(sa.delete(table).where(table.c.run_id.in_(run_ids)))
        run(sa.delete(ri).where(ri.c.run_id.in_(run_ids)))
    run(sa.delete(ri).where(ri.c.session_id == session_id))
    if dead_runs:
        connection.execute(
            sa.update(ms).where(ms.c.run_id.in_(dead_runs)).values(run_id=None)
        )
        run(sa.delete(rn).where(rn.c.run_id.in_(dead_runs)))
    keep_messages = (
        sa.select(rn.c.trigger_message_id).where(rn.c.run_id.in_(kept_runs))
        if kept_runs
        else None
    )
    messages = sa.delete(ms).where(ms.c.session_id == session_id)
    if keep_messages is not None:
        messages = messages.where(ms.c.message_id.not_in(keep_messages))
    run(messages)
    return deleted > 0
