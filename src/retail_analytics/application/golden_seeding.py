"""Load the project-authored Golden seeds through the ordinary lifecycle.

Seeds enter exactly like any other candidate: the author identity submits a
candidate, and a different reviewer identity approves it. Nothing is written
around the lifecycle, so every seed has a review history, the independent
reviewer rule applies, and the origin stays ``project_authored`` rather than
posing as a historical analyst record.

Reseeding is idempotent. Example identifiers and idempotency keys are stable,
so a repeat submit returns the stored version; only a version still waiting as
a candidate is approved. A version that a reviewer has since suspended,
rejected, retired or erased is reported and left alone, never reinstated by a
rerun. Changing a seed's content means bumping ``SEED_LIBRARY_REVISION``, which
creates the next version of the same example.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from retail_analytics.application.authorization import Principal
from retail_analytics.application.golden_seed_library import (
    SEED_LIBRARY_REVISION,
    SEED_SCHEMA_VERSION,
    SeedExample,
    check_library,
    seed_library,
)
from retail_analytics.application.knowledge import (
    ApprovalChecks,
    ExampleDraft,
    KnowledgeError,
    KnowledgeErrorCode,
    KnowledgeService,
)
from retail_analytics.domain.access import Permission
from retail_analytics.domain.catalog import LogicalCatalog
from retail_analytics.domain.knowledge import (
    Applicability,
    ExampleRef,
    KnowledgeAccess,
    Origin,
    Provenance,
    ReviewStatus,
    SourceKind,
)
from retail_analytics.domain.logical_catalog import default_logical_catalog
from retail_analytics.domain.metrics import MetricCatalog, default_catalog

APPROVAL_RATIONALE = (
    f"Seed library revision {SEED_LIBRARY_REVISION}, approved by the local "
    "development reviewer identity after the repository's seed validation "
    "checks passed (SQL compiles against the logical catalog, fixture figures "
    "recomputed, sanitization screen) and following the written seed review "
    "checklist. Project-authored teaching example; not a historical analyst "
    "record."
)


class SeedLibraryInvalid(Exception):
    """The library failed its own checks; nothing is submitted."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = tuple(problems)
        super().__init__("seed library is not fit to publish: " + "; ".join(problems))


class SeedOutcome(StrEnum):
    PUBLISHED = "published"
    ALREADY_PUBLISHED = "already_published"
    # A reviewer's later decision (suspend, reject, retire, erase) is kept.
    LEFT_UNCHANGED = "left_unchanged"
    # The key exists with different content: revision was not bumped.
    CONTENT_CONFLICT = "content_conflict"


@dataclass(frozen=True, slots=True)
class SeedResult:
    key: str
    outcome: SeedOutcome
    ref: ExampleRef | None
    status: ReviewStatus | None


def seed_principals(
    author_executive: str, reviewer_executive: str
) -> tuple[Principal, Principal]:
    """Author may analyse; reviewer may review. Scopes are only a ceiling."""
    return (
        Principal(author_executive, frozenset({Permission.ANALYSIS_READ.value})),
        Principal(reviewer_executive, frozenset({Permission.KNOWLEDGE_REVIEW.value})),
    )


def draft_for(example: SeedExample) -> ExampleDraft:
    metrics = example.metrics
    return ExampleDraft(
        question=example.question,
        sql=example.sql,
        method_summary=example.method_summary,
        report_markdown=example.report_markdown,
        applicability=Applicability(SEED_SCHEMA_VERSION, metrics),
        access=KnowledgeAccess.shared(),
        origin=Origin.PROJECT_AUTHORED,
        provenance=Provenance(SourceKind.AUTHORED),
        # The content is invented for this project from the catalogs; it is
        # screened on submission and reviewed on approval.
        sanitization_attested=True,
    )


async def seed_golden_library(
    service: KnowledgeService,
    author: Principal,
    reviewer: Principal,
    *,
    library: Sequence[SeedExample] | None = None,
    logical: LogicalCatalog | None = None,
    metrics: MetricCatalog | None = None,
) -> tuple[SeedResult, ...]:
    """Submit and publish every seed; safe to run any number of times."""
    examples = tuple(library if library is not None else seed_library())
    problems = check_library(
        examples, logical or default_logical_catalog(), metrics or default_catalog()
    )
    if problems:
        raise SeedLibraryInvalid(problems)
    return tuple([await _seed_one(service, author, reviewer, e) for e in examples])


async def _seed_one(
    service: KnowledgeService,
    author: Principal,
    reviewer: Principal,
    example: SeedExample,
) -> SeedResult:
    try:
        version = await service.submit_candidate(
            author,
            draft_for(example),
            idempotency_key=example.idempotency_key,
            example_id=example.example_id,
        )
    except KnowledgeError as error:
        if error.code is KnowledgeErrorCode.IDEMPOTENCY_CONFLICT:
            return SeedResult(example.key, SeedOutcome.CONTENT_CONFLICT, None, None)
        raise
    if version.status is ReviewStatus.PUBLISHED:
        return SeedResult(
            example.key, SeedOutcome.ALREADY_PUBLISHED, version.ref, version.status
        )
    if version.status is not ReviewStatus.CANDIDATE:
        return SeedResult(
            example.key, SeedOutcome.LEFT_UNCHANGED, version.ref, version.status
        )
    result = await service.approve(
        reviewer,
        version.ref,
        rationale=APPROVAL_RATIONALE,
        checks=ApprovalChecks(correct=True, sanitized=True, applicable=True),
    )
    return SeedResult(
        example.key, SeedOutcome.PUBLISHED, result.version.ref, result.version.status
    )
