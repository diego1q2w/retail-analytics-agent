from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import datetime
from typing import Protocol

from retail_analytics.application.contracts.evidence import (
    DEFAULT_CANDIDATE_LIMIT,
    ImportedEvidence,
    NewEvidence,
    NewEvidenceImport,
    PendingReuseLink,
    ReuseLinkVerdict,
    ReuseRevalidation,
    RunEvidenceLink,
    StoredEvidence,
)
from retail_analytics.domain.access import ProductScope
from retail_analytics.domain.evidence import (
    Evidence,
    EvidenceUse,
    PinHolder,
)


class EvidenceRepository(Protocol):
    """Immutable evidence records and which runs used them."""

    async def record(self, new: NewEvidence) -> Evidence:
        """Store once per operation and link it to its run as produced.

        Repeating the same operation with the same content returns the stored
        record; different content raises ``IdempotencyConflict``. A refresh of
        evidence owned by someone else, or in another session, raises
        ``AccessDenied``.
        """
        ...

    async def get(self, evidence_id: str) -> StoredEvidence | None: ...

    async def candidates(
        self,
        executive_id: str,
        session_id: str,
        *,
        subject_key: str | None = None,
        limit: int = DEFAULT_CANDIDATE_LIMIT,
    ) -> Sequence[StoredEvidence]:
        """The executive's evidence in that session, newest computation first."""
        ...

    async def link_run(self, run_id: str, evidence_id: str, use: EvidenceUse) -> None:
        """Record that a run used the evidence (repeating is a no-op)."""
        ...

    async def for_run(self, run_id: str) -> Sequence[RunEvidenceLink]: ...


class SessionEvidenceImports(Protocol):
    """Saved-report evidence linked into the owner's other sessions."""

    async def add_import(self, new: NewEvidenceImport) -> bool:
        """Record the link through ``new.report_id`` and link the evidence
        to ``new.run_id`` as reused, in one transaction. The evidence, the
        session and the report must belong to ``new.executive_id``, else
        ``AccessDenied``.

        Repeating a live link is a no-op. The caller has just run every reuse
        check, so a withdrawn link of a live report is reinstated (recorded as
        a re-validation). Returns False, linking nothing, when the report has
        been soft-deleted meanwhile (checked under the report's row lock, so a
        concurrent deletion cannot be undone by a late import).
        """
        ...

    async def imported(
        self,
        executive_id: str,
        session_id: str,
        *,
        evidence_id: str | None = None,
        subject_key: str | None = None,
        limit: int = DEFAULT_CANDIDATE_LIMIT,
    ) -> Sequence[ImportedEvidence]:
        """The executive's report evidence linked into that session, newest
        import first (optionally one record or one subject)."""
        ...


class ReuseLinkRevalidation(Protocol):
    """Re-validation of a restored report's withdrawn links (T18-F5)."""

    async def pending_links(
        self, owner_id: str, report_id: str
    ) -> Sequence[PendingReuseLink]:
        """The owner's links through this report awaiting re-validation."""
        ...

    async def record_revalidation(
        self,
        owner_id: str,
        report_id: str,
        verdicts: Sequence[ReuseLinkVerdict],
        *,
        at: datetime,
        actor_id: str,
        audit_id: str,
    ) -> ReuseRevalidation:
        """Apply the verdicts to links still pending, with one audit event,
        in one transaction: reinstate the passing ones, mark the others
        failed. Nothing is reinstated if the report was deleted again."""
        ...


class EvidencePins(Protocol):
    """Retention holds, e.g. a saved report keeping its supporting snapshots."""

    async def pin(
        self, executive_id: str, evidence_ids: Sequence[str], holder: PinHolder
    ) -> None:
        """Pin all or nothing; any unknown or not-owned ID raises AccessDenied."""
        ...

    async def unpin(self, holder: PinHolder) -> int:
        """Release every pin of the holder; returns how many were removed."""
        ...

    async def holders(self, evidence_id: str) -> tuple[PinHolder, ...]: ...

    async def pinned(self, holder: PinHolder) -> tuple[str, ...]: ...


class ProductScopeSnapshots(Protocol):
    """Exact product sets behind evidence authority stamps, keyed by digest.

    Written only by trusted code (with the evidence record, from the execution
    context's ``ProductScope``). The product IDs never leave the store: callers
    ask coverage questions and get digests back.
    """

    async def combine(self, digests: Collection[str]) -> str | None:
        """Record the union of these recorded sets and return its digest, or
        None when any of them is not on record (the exact set is unknown)."""
        ...

    async def covered(
        self, digests: Collection[str], scope: ProductScope
    ) -> frozenset[str]:
        """The given digests whose recorded set is a subset of ``scope``'s
        products. Unknown digests are never covered."""
        ...


class QueryGrainAudit(Protocol):
    """Re-checks a stored logical query against the current demographic rule.

    Used for evidence recorded before demographics became aggregate-only.
    """

    def aggregate_only(self, logical_sql: str) -> bool:
        """True only when the query provably uses customer demographics as
        group-level statistics (no identity grain, targeting or identity
        output). Anything unparsable or unverifiable is False."""
        ...
