from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from retail_analytics.application.contracts.evidence import (
    DEFAULT_CANDIDATE_LIMIT,
    NewEvidence,
    RunEvidenceLink,
    StoredEvidence,
)
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
