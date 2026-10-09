from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Protocol

from retail_analytics.application.contracts.evidence import (
    DEFAULT_CANDIDATE_LIMIT,
    NewEvidence,
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
