"""PostgreSQL ``ReportDeletionRepository``.

Locking order, used by every operation here: the owner or proposal row first,
then the per-report advisory locks in sorted ID order (the same lock
``PostgresReportRepository.add_version`` takes), then the report rows in
sorted order. A new version therefore cannot slip in between the version check
and the deletion, and two confirmations cannot interleave.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

import sqlalchemy as sa

from retail_analytics.adapters.postgres.audit import append_audit
from retail_analytics.adapters.postgres.database import Database
from retail_analytics.adapters.postgres.schema import deletion_proposal_items as di
from retail_analytics.adapters.postgres.schema import deletion_proposals as dp
from retail_analytics.adapters.postgres.schema import report_versions as rv
from retail_analytics.adapters.postgres.schema import reports as rp
from retail_analytics.application.authorization import AccessDenied
from retail_analytics.application.contracts.audit import AuditEvent
from retail_analytics.application.contracts.report_deletion import NewDeletionProposal
from retail_analytics.domain.report_deletion import (
    CANCELLED,
    CONFIRMED,
    MAX_PENDING_PROPOSALS,
    PROPOSED,
    CurrentReport,
    DeletionError,
    DeletionErrorCode,
    DeletionProposal,
    ProposalItem,
    ProposalStatus,
    check_confirmable,
    request_digest,
)

_SUBJECT = "deletion_proposal"


def _lock_reports(connection: sa.Connection, report_ids: Sequence[str]) -> None:
    for report_id in sorted(report_ids):
        connection.execute(
            sa.select(
                sa.func.pg_advisory_xact_lock(sa.func.hashtextextended(report_id, 0))
            )
        )


def _items(connection: sa.Connection, proposal_id: str) -> tuple[ProposalItem, ...]:
    rows = connection.execute(
        sa.select(
            di.c.report_id,
            di.c.version,
            rv.c.title,
            rv.c.created_at,
            rv.c.scope_digest,
        )
        .select_from(
            di.join(
                rv,
                sa.and_(rv.c.report_id == di.c.report_id, rv.c.version == di.c.version),
            )
        )
        .where(di.c.proposal_id == proposal_id)
        .order_by(di.c.ordinal)
    )
    return tuple(
        ProposalItem(r.report_id, r.version, r.title, r.created_at, r.scope_digest)
        for r in rows
    )


def _proposal(connection: sa.Connection, row: sa.Row[Any]) -> DeletionProposal:
    return DeletionProposal(
        proposal_id=row.proposal_id,
        owner_id=row.owner_id,
        session_id=row.session_id,
        run_id=row.run_id,
        status=ProposalStatus(row.status),
        created_at=row.created_at,
        expires_at=row.expires_at,
        resolved_at=row.resolved_at,
        items=_items(connection, row.proposal_id),
    )


def _details(proposal: DeletionProposal) -> dict[str, object]:
    return {
        "proposal_id": proposal.proposal_id,
        "report_count": len(proposal.items),
        "reports": [
            {"report_id": i.report_id, "version": i.version} for i in proposal.items
        ],
        "expires_at": proposal.expires_at.isoformat(),
    }


def _audit(
    proposal: DeletionProposal, action: str, audit_id: str, at: datetime
) -> AuditEvent:
    return AuditEvent(
        audit_id=audit_id,
        occurred_at=at,
        actor_id=proposal.owner_id,
        action=action,
        subject_type=_SUBJECT,
        subject_id=proposal.proposal_id,
        session_id=proposal.session_id,
        run_id=proposal.run_id,
        details=_details(proposal),
    )


class PostgresReportDeletionRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def propose(self, new: NewDeletionProposal) -> tuple[DeletionProposal, bool]:
        return await self._db.transaction(self._propose, new)

    async def get(self, owner_id: str, proposal_id: str) -> DeletionProposal:
        return await self._db.transaction(self._get, owner_id, proposal_id)

    async def confirm(
        self, owner_id: str, proposal_id: str, *, at: datetime, audit_id: str
    ) -> DeletionProposal:
        return await self._db.transaction(
            self._confirm, owner_id, proposal_id, at, audit_id
        )

    async def cancel(
        self, owner_id: str, proposal_id: str, *, at: datetime, audit_id: str
    ) -> DeletionProposal:
        return await self._db.transaction(
            self._cancel, owner_id, proposal_id, at, audit_id
        )

    @staticmethod
    def _get(
        connection: sa.Connection, owner_id: str, proposal_id: str
    ) -> DeletionProposal:
        row = connection.execute(
            sa.select(dp).where(
                dp.c.proposal_id == proposal_id, dp.c.owner_id == owner_id
            )
        ).one_or_none()
        if row is None:
            raise AccessDenied(_SUBJECT, proposal_id)
        return _proposal(connection, row)

    @staticmethod
    def _propose(
        connection: sa.Connection, new: NewDeletionProposal
    ) -> tuple[DeletionProposal, bool]:
        connection.execute(
            sa.select(
                sa.func.pg_advisory_xact_lock(
                    sa.func.hashtextextended(f"deletion:{new.owner_id}", 0)
                )
            )
        )
        digest = request_digest(new.report_ids)
        prior = connection.execute(
            sa.select(dp).where(
                dp.c.owner_id == new.owner_id,
                dp.c.idempotency_key == new.idempotency_key,
            )
        ).one_or_none()
        if prior is not None:
            if prior.request_digest != digest:
                raise DeletionError(
                    DeletionErrorCode.IDEMPOTENCY_CONFLICT,
                    "this operation already proposed a different deletion",
                )
            return _proposal(connection, prior), False

        pending = connection.execute(
            sa.select(sa.func.count())
            .select_from(dp)
            .where(
                dp.c.owner_id == new.owner_id,
                dp.c.status == ProposalStatus.PENDING.value,
                dp.c.expires_at > new.created_at,
            )
        ).scalar_one()
        if pending >= MAX_PENDING_PROPOSALS:
            raise DeletionError(
                DeletionErrorCode.TOO_MANY_PENDING,
                "too many deletions are waiting for confirmation; "
                "confirm or cancel some first",
            )

        _lock_reports(connection, new.report_ids)
        latest: dict[str, int] = {}
        for report_id in sorted(new.report_ids):
            version = connection.execute(
                sa.select(sa.func.max(rv.c.version))
                .select_from(rv.join(rp, rp.c.report_id == rv.c.report_id))
                .where(
                    rp.c.report_id == report_id,
                    rp.c.owner_id == new.owner_id,
                    rp.c.deleted_at.is_(None),
                )
            ).scalar_one()
            if version is None:
                raise AccessDenied("report", report_id)
            latest[report_id] = version

        connection.execute(
            sa.insert(dp).values(
                proposal_id=new.proposal_id,
                owner_id=new.owner_id,
                session_id=new.session_id,
                run_id=new.run_id,
                idempotency_key=new.idempotency_key,
                request_digest=digest,
                status=ProposalStatus.PENDING.value,
                created_at=new.created_at,
                expires_at=new.expires_at,
            )
        )
        connection.execute(
            sa.insert(di),
            [
                {
                    "proposal_id": new.proposal_id,
                    "report_id": report_id,
                    "version": latest[report_id],
                    "ordinal": ordinal,
                }
                for ordinal, report_id in enumerate(new.report_ids)
            ],
        )
        stored = PostgresReportDeletionRepository._get(
            connection, new.owner_id, new.proposal_id
        )
        append_audit(connection, _audit(stored, PROPOSED, new.audit_id, new.created_at))
        return stored, True

    @staticmethod
    def _lock_proposal(
        connection: sa.Connection, owner_id: str, proposal_id: str
    ) -> DeletionProposal:
        row = connection.execute(
            sa.select(dp)
            .where(dp.c.proposal_id == proposal_id, dp.c.owner_id == owner_id)
            .with_for_update()
        ).one_or_none()
        if row is None:
            raise AccessDenied(_SUBJECT, proposal_id)
        return _proposal(connection, row)

    @staticmethod
    def _confirm(
        connection: sa.Connection,
        owner_id: str,
        proposal_id: str,
        at: datetime,
        audit_id: str,
    ) -> DeletionProposal:
        proposal = PostgresReportDeletionRepository._lock_proposal(
            connection, owner_id, proposal_id
        )
        # Cheap rejections first, so a consumed or expired proposal never
        # takes report locks.
        if proposal.status is not ProposalStatus.PENDING or proposal.expired_at(at):
            check_confirmable(proposal, {}, at)
        ids = [i.report_id for i in proposal.items]
        _lock_reports(connection, ids)
        rows = connection.execute(
            sa.select(rp.c.report_id, rp.c.owner_id, rp.c.deleted_at)
            .where(rp.c.report_id.in_(ids))
            .order_by(rp.c.report_id)
            .with_for_update()
        ).all()
        newest = {
            r.report_id: r.latest
            for r in connection.execute(
                sa.select(rv.c.report_id, sa.func.max(rv.c.version).label("latest"))
                .where(rv.c.report_id.in_(ids))
                .group_by(rv.c.report_id)
            )
        }
        current = {
            r.report_id: CurrentReport(
                r.owner_id, newest.get(r.report_id, 0), r.deleted_at is not None
            )
            for r in rows
        }
        check_confirmable(proposal, current, at)

        connection.execute(
            sa.update(rp)
            .where(
                rp.c.report_id.in_(ids),
                rp.c.owner_id == owner_id,
                rp.c.deleted_at.is_(None),
            )
            .values(deleted_at=at)
        )
        connection.execute(
            sa.update(dp)
            .where(
                dp.c.proposal_id == proposal_id,
                dp.c.status == ProposalStatus.PENDING.value,
            )
            .values(status=ProposalStatus.CONFIRMED.value, resolved_at=at)
        )
        resolved = PostgresReportDeletionRepository._get(
            connection, owner_id, proposal_id
        )
        append_audit(connection, _audit(resolved, CONFIRMED, audit_id, at))
        return resolved

    @staticmethod
    def _cancel(
        connection: sa.Connection,
        owner_id: str,
        proposal_id: str,
        at: datetime,
        audit_id: str,
    ) -> DeletionProposal:
        proposal = PostgresReportDeletionRepository._lock_proposal(
            connection, owner_id, proposal_id
        )
        if proposal.status is not ProposalStatus.PENDING:
            raise DeletionError(
                DeletionErrorCode.ALREADY_RESOLVED,
                "this proposal was already confirmed or cancelled",
            )
        connection.execute(
            sa.update(dp)
            .where(dp.c.proposal_id == proposal_id)
            .values(status=ProposalStatus.CANCELLED.value, resolved_at=at)
        )
        resolved = PostgresReportDeletionRepository._get(
            connection, owner_id, proposal_id
        )
        append_audit(connection, _audit(resolved, CANCELLED, audit_id, at))
        return resolved
