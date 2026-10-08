from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from retail_analytics.application.contracts.knowledge import (
    ChangeResult,
    IndexChange,
    IndexDocument,
    NewCandidate,
    ReviewEvent,
    StatusChange,
)
from retail_analytics.domain.knowledge import (
    ExampleRef,
    GoldenVersion,
    ReviewStatus,
    SourceKind,
)


class KnowledgeRepository(Protocol):
    async def add_candidate(self, candidate: NewCandidate) -> GoldenVersion:
        """Append the example's next version (serialized per example).

        A repeated (author, idempotency key) returns the original version. A
        different author for an existing example raises ``AccessDenied``.
        """
        ...

    async def get(self, ref: ExampleRef) -> GoldenVersion | None: ...

    async def apply(self, change: StatusChange) -> ChangeResult:
        """Apply ``change`` with its audit event and index events, atomically.

        Raises ``KnowledgeError`` (``NOT_FOUND``, ``CONFLICT``) when the
        version is missing, no longer in the expected status, or publishing
        would leave two published versions.
        """
        ...

    async def by_source(
        self, source_kind: SourceKind, source_id: str
    ) -> Sequence[GoldenVersion]: ...

    async def by_status(
        self, status: ReviewStatus, limit: int
    ) -> Sequence[GoldenVersion]: ...

    async def events(self, ref: ExampleRef) -> Sequence[ReviewEvent]: ...

    async def pending_purges(self, limit: int) -> Sequence[tuple[ExampleRef, str]]:
        """Erased versions whose report artifact is not yet purged."""
        ...

    async def purge_done(self, ref: ExampleRef) -> None: ...


class KnowledgeIndexSource(Protocol):
    """Trusted read side for retrieval indexes (T24) and their invalidation."""

    async def published_documents(
        self, after: ExampleRef | None, limit: int
    ) -> Sequence[IndexDocument]: ...

    async def changes_after(
        self, sequence: int, limit: int
    ) -> Sequence[IndexChange]: ...
