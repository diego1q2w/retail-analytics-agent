from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from retail_analytics.domain.knowledge import (
    Applicability,
    ExampleContent,
    ExampleRef,
    GoldenVersion,
    IndexChangeKind,
    KnowledgeAccess,
    Origin,
    Provenance,
    ReviewAction,
    ReviewStatus,
)


@dataclass(frozen=True, slots=True)
class ReviewEvent:
    example_id: str
    version: int
    action: ReviewAction
    actor_id: str
    from_status: ReviewStatus | None
    to_status: ReviewStatus
    rationale: str
    checks: Mapping[str, bool] | None
    at: datetime


@dataclass(frozen=True, slots=True)
class IndexChange:
    """One ordered invalidation fact for retrieval indexes. Holds no content."""

    sequence: int
    example_id: str
    version: int
    kind: IndexChangeKind
    reason: ReviewAction
    at: datetime


@dataclass(frozen=True, slots=True)
class IndexDocument:
    """What a retrieval index builds from: question plus reviewed method.

    ``access`` and ``applicability`` must be kept with the entry; embeddings do
    not remove restrictions.
    """

    ref: ExampleRef
    content_digest: str
    question: str
    method_summary: str
    access: KnowledgeAccess
    applicability: Applicability


@dataclass(frozen=True, slots=True)
class NewCandidate:
    example_id: str
    author_id: str
    idempotency_key: str
    origin: Origin
    access: KnowledgeAccess
    applicability: Applicability
    provenance: Provenance
    content: ExampleContent
    content_digest: str
    at: datetime


@dataclass(frozen=True, slots=True)
class StatusChange:
    """One atomic change; the repository checks ``expected`` under a lock."""

    ref: ExampleRef
    expected: ReviewStatus
    action: ReviewAction
    to_status: ReviewStatus
    index: IndexChangeKind | None
    actor_id: str
    rationale: str
    checks: Mapping[str, bool] | None
    at: datetime
    # Retire the example's other published/suspended versions in the same
    # transaction (publishing a newer version).
    supersede_others: bool = False


@dataclass(frozen=True, slots=True)
class ChangeResult:
    version: GoldenVersion
    superseded: tuple[ExampleRef, ...] = ()
