"""In-memory deletion repository obeying the PostgreSQL repository's contract.

It reuses the domain's ``check_confirmable`` and applies changes only after the
check passes, so a failed confirmation leaves everything untouched.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime

from retail_analytics.application.authorization import AccessDenied
from retail_analytics.application.contracts.audit import AuditEvent
from retail_analytics.application.contracts.report_deletion import NewDeletionProposal
from retail_analytics.domain.report_deletion import (
    CurrentReport,
    DeletionError,
    DeletionErrorCode,
    DeletionProposal,
    ProposalItem,
    ProposalStatus,
    check_confirmable,
    request_digest,
)
from tests.unit.reports.fakes import FakeReportRepository


@dataclass
class FakeDeletionRepository:
    reports: FakeReportRepository
    proposals: dict[str, DeletionProposal] = field(default_factory=dict)
    keys: dict[tuple[str, str], tuple[str, str]] = field(default_factory=dict)
    audit: list[AuditEvent] = field(default_factory=list)
    fail_audit: bool = False

    def _latest(self, report_id: str) -> int:
        return max(r.version for r in self.reports.rows if r.report_id == report_id)

    async def propose(self, new: NewDeletionProposal) -> tuple[DeletionProposal, bool]:
        digest = request_digest(new.report_ids)
        if (new.owner_id, new.idempotency_key) in self.keys:
            proposal_id, prior = self.keys[(new.owner_id, new.idempotency_key)]
            if prior != digest:
                raise DeletionError(DeletionErrorCode.IDEMPOTENCY_CONFLICT, "x")
            return self.proposals[proposal_id], False
        items = []
        for report_id in new.report_ids:
            latest = await self.reports.get(new.owner_id, report_id)
            if latest is None:
                raise AccessDenied("report", report_id)
            items.append(
                ProposalItem(
                    report_id,
                    latest.version,
                    latest.title,
                    latest.created_at,
                    latest.scope_digest,
                    latest.required_scope_digest,
                )
            )
        proposal = DeletionProposal(
            new.proposal_id,
            new.owner_id,
            new.session_id,
            new.run_id,
            ProposalStatus.PENDING,
            new.created_at,
            new.expires_at,
            None,
            tuple(items),
        )
        self.proposals[new.proposal_id] = proposal
        self.keys[(new.owner_id, new.idempotency_key)] = (new.proposal_id, digest)
        return proposal, True

    async def get(self, owner_id: str, proposal_id: str) -> DeletionProposal:
        proposal = self.proposals.get(proposal_id)
        if proposal is None or proposal.owner_id != owner_id:
            raise AccessDenied("deletion_proposal", proposal_id)
        return proposal

    async def list_pending(
        self, owner_id: str, *, at: datetime, limit: int
    ) -> tuple[DeletionProposal, ...]:
        found = [
            p
            for p in self.proposals.values()
            if p.owner_id == owner_id
            and p.status is ProposalStatus.PENDING
            and p.expires_at > at
        ]
        found.sort(key=lambda p: p.created_at, reverse=True)
        return tuple(found[:limit])

    async def confirm(
        self, owner_id: str, proposal_id: str, *, at: datetime, audit_id: str
    ) -> DeletionProposal:
        proposal = await self.get(owner_id, proposal_id)
        current = {
            i.report_id: CurrentReport(
                owner_id=next(
                    r.owner_id for r in self.reports.rows if r.report_id == i.report_id
                ),
                latest_version=self._latest(i.report_id),
                deleted=i.report_id in self.reports.deleted,
            )
            for i in proposal.items
        }
        check_confirmable(proposal, current, at)
        if self.fail_audit:
            raise RuntimeError("audit insert failed")
        self.reports.deleted.update(i.report_id for i in proposal.items)
        done = replace(proposal, status=ProposalStatus.CONFIRMED, resolved_at=at)
        self.proposals[proposal_id] = done
        self.audit.append(
            AuditEvent(audit_id, at, owner_id, "confirmed", "p", proposal_id)
        )
        return done

    async def cancel(
        self, owner_id: str, proposal_id: str, *, at: datetime, audit_id: str
    ) -> DeletionProposal:
        proposal = await self.get(owner_id, proposal_id)
        if proposal.status is not ProposalStatus.PENDING:
            raise DeletionError(DeletionErrorCode.ALREADY_RESOLVED, "x")
        done = replace(proposal, status=ProposalStatus.CANCELLED, resolved_at=at)
        self.proposals[proposal_id] = done
        return done
