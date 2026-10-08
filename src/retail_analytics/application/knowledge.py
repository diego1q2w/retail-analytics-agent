"""Golden Knowledge use cases: submit, review, deliver, invalidate, erase.

Three entry points, each with a different trust level:

- ``KnowledgeService`` takes an authenticated ``Principal``. Authors need
  ``analysis:read``; every review command needs ``knowledge:review`` and, for
  approve/reject/reinstate, must come from someone other than the author.
- ``GoldenKnowledgeReader`` is the only way examples reach the model. It takes
  the trusted ``ProductScope`` from the run's context and re-checks status,
  access, compatibility and the pinned content digest at delivery time, so a
  stale index candidate can never leak a retired, suspended, erased or
  out-of-scope example.
- ``KnowledgeIndexSource`` (trusted, never a model tool) exposes published
  documents and an ordered feed of index invalidation events for retrieval
  indexes to follow.

Content is screened for direct personal data and identifier lists before it is
stored. Sanitization is also attested by the author and confirmed by the
reviewer; the screen supplements, and does not replace, that review. An
example grants no product access: restricted examples are only delivered to
executives whose own current scope already covers the products.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from retail_analytics.application.artifacts import (
    ArtifactError,
    ArtifactMaintenance,
    ArtifactService,
)
from retail_analytics.application.authorization import (
    AccessDenied,
    AccessResolver,
)
from retail_analytics.application.contracts.authorization import Principal
from retail_analytics.application.contracts.knowledge import (
    ChangeResult,
    NewCandidate,
    ReviewEvent,
    StatusChange,
)
from retail_analytics.application.ports.knowledge import KnowledgeRepository
from retail_analytics.domain.access import Permission, ProductScope
from retail_analytics.domain.artifacts import MARKDOWN
from retail_analytics.domain.knowledge import (
    MAX_RATIONALE,
    OPAQUE_ID_PATTERN,
    Applicability,
    ApplicabilityContext,
    DeliveryRefusal,
    ErasureReason,
    ExampleContent,
    ExampleRef,
    GoldenVersion,
    KnowledgeAccess,
    Origin,
    Provenance,
    ReviewAction,
    ReviewStatus,
    SelfReview,
    SourceKind,
    content_digest,
    decide,
    is_valid_example_id,
)
from retail_analytics.domain.sensitive_content import screen_fields

# Owner of the stored report of every example: shared knowledge belongs to no
# executive, and no executive can read it through the artifact service.
KNOWLEDGE_OWNER = "system:golden-knowledge"
IDEMPOTENCY_KEY_PATTERN = OPAQUE_ID_PATTERN


class KnowledgeErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    SENSITIVE_CONTENT = "sensitive_content"
    NOT_FOUND = "not_found"
    SELF_REVIEW = "self_review"
    CONFLICT = "conflict"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    CONTENT_UNAVAILABLE = "content_unavailable"


class KnowledgeError(Exception):
    """Structured failure. ``findings`` names fields and kinds, never text."""

    def __init__(
        self,
        code: KnowledgeErrorCode,
        message: str,
        *,
        findings: Sequence[tuple[str, str]] = (),
    ) -> None:
        self.code = code
        self.message = message
        self.findings = tuple(findings)
        super().__init__(f"{code.value}: {message}")


@dataclass(frozen=True, slots=True)
class ExampleDraft:
    """A proposed trio. Everything here is screened and then reviewed."""

    question: str
    sql: str
    method_summary: str
    report_markdown: str
    applicability: Applicability
    access: KnowledgeAccess
    origin: Origin
    provenance: Provenance
    # The author confirms the content is sanitized (question, SQL literals and
    # report); required for shared examples.
    sanitization_attested: bool = False


@dataclass(frozen=True, slots=True)
class ApprovalChecks:
    """What the reviewer confirms before publishing (all must be true)."""

    correct: bool
    sanitized: bool
    applicable: bool

    @property
    def complete(self) -> bool:
        return self.correct and self.sanitized and self.applicable


@dataclass(frozen=True, slots=True)
class GoldenExample:
    """A delivered example. No author, reviewer or source identifiers."""

    ref: ExampleRef
    content_digest: str
    origin: Origin
    question: str
    sql: str
    method_summary: str
    report_markdown: str
    applicability: Applicability
    shared: bool


@dataclass(frozen=True, slots=True)
class Refused:
    ref: ExampleRef
    reason: DeliveryRefusal


@dataclass(frozen=True, slots=True)
class Delivery:
    examples: tuple[GoldenExample, ...]
    refused: tuple[Refused, ...]


@dataclass(frozen=True, slots=True)
class CandidateSummary:
    """Review-queue entry: metadata only, no content."""

    ref: ExampleRef
    origin: Origin
    author_id: str
    shared: bool
    schema_version: str
    created_at: datetime


_CONTENT_JUDGEMENTS = frozenset(
    {ReviewAction.APPROVE, ReviewAction.REJECT, ReviewAction.REINSTATE}
)


class _Store:
    """Shared plumbing: stored report bytes behind the artifact service."""

    def __init__(self, artifacts: ArtifactService) -> None:
        self._artifacts = artifacts

    async def _example(self, version: GoldenVersion) -> GoldenExample:
        content = version.content
        if content is None or version.content_digest is None:
            raise KnowledgeError(
                KnowledgeErrorCode.CONTENT_UNAVAILABLE, "content is erased"
            )
        try:
            report = await self._artifacts.read(
                KNOWLEDGE_OWNER,
                content.report_artifact_id,
                content.report_artifact_version,
            )
        except (ArtifactError, AccessDenied):
            raise KnowledgeError(
                KnowledgeErrorCode.CONTENT_UNAVAILABLE, "stored report unavailable"
            ) from None
        return GoldenExample(
            ref=version.ref,
            content_digest=version.content_digest,
            origin=version.origin,
            question=content.question,
            sql=content.sql,
            method_summary=content.method_summary,
            report_markdown=report.content.decode("utf-8"),
            applicability=version.applicability,
            shared=version.access.is_shared,
        )


def _clean_rationale(text: str) -> str:
    rationale = text.strip()
    if not 0 < len(rationale) <= MAX_RATIONALE:
        raise KnowledgeError(
            KnowledgeErrorCode.INVALID_REQUEST,
            f"rationale must be 1-{MAX_RATIONALE} characters",
        )
    findings = screen_fields({"rationale": rationale})
    if findings:
        raise KnowledgeError(
            KnowledgeErrorCode.SENSITIVE_CONTENT,
            "rationale contains sensitive content",
            findings=[(f, k.value) for f, k in findings],
        )
    return rationale


class KnowledgeService(_Store):
    """Author and reviewer commands."""

    def __init__(
        self,
        resolver: AccessResolver,
        repository: KnowledgeRepository,
        artifacts: ArtifactService,
        maintenance: ArtifactMaintenance,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        new_id: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        super().__init__(artifacts)
        self._resolver = resolver
        self._repo = repository
        self._maintenance = maintenance
        self._clock = clock
        self._new_id = new_id

    # -- authoring ---------------------------------------------------------

    async def submit_candidate(
        self,
        principal: Principal,
        draft: ExampleDraft,
        *,
        idempotency_key: str,
        example_id: str | None = None,
    ) -> GoldenVersion:
        """Propose a trio as a candidate (version 1, or the next of an example).

        Candidates are never delivered. Repeating ``idempotency_key`` with the
        same content returns the original version.
        """
        access = await self._resolver.require_permission(
            principal, Permission.ANALYSIS_READ
        )
        author = access.executive_id
        if IDEMPOTENCY_KEY_PATTERN.fullmatch(idempotency_key) is None:
            raise KnowledgeError(
                KnowledgeErrorCode.INVALID_REQUEST, "invalid idempotency key"
            )
        if example_id is not None and not is_valid_example_id(example_id):
            raise KnowledgeError(KnowledgeErrorCode.INVALID_REQUEST, "invalid example")
        if draft.access.is_shared and not draft.sanitization_attested:
            raise KnowledgeError(
                KnowledgeErrorCode.INVALID_REQUEST,
                "shared examples need the author's sanitization attestation",
            )
        if draft.access.restricted_product_ids and not (
            draft.access.restricted_product_ids <= access.product_scope.product_ids
        ):
            raise AccessDenied("product", "restricted scope")
        self._screen(draft)
        try:
            content, report_sha = await self._store_report(
                author, idempotency_key, draft
            )
        except ValueError as error:
            raise KnowledgeError(
                KnowledgeErrorCode.INVALID_REQUEST, "invalid example content"
            ) from error
        digest = content_digest(
            draft.question, draft.sql, draft.method_summary, report_sha
        )
        stored = await self._repo.add_candidate(
            NewCandidate(
                example_id=example_id or self._new_id(),
                author_id=author,
                idempotency_key=idempotency_key,
                origin=draft.origin,
                access=draft.access,
                applicability=draft.applicability,
                provenance=draft.provenance,
                content=content,
                content_digest=digest,
                at=self._clock(),
            )
        )
        if stored.content_digest != digest:
            raise KnowledgeError(
                KnowledgeErrorCode.IDEMPOTENCY_CONFLICT,
                "idempotency key was used for different content",
            )
        return stored

    @staticmethod
    def _screen(draft: ExampleDraft) -> None:
        findings = screen_fields(
            {
                "question": draft.question,
                "sql": draft.sql,
                "method_summary": draft.method_summary,
                "report": draft.report_markdown,
            }
        )
        if findings:
            raise KnowledgeError(
                KnowledgeErrorCode.SENSITIVE_CONTENT,
                "content contains data that must not be stored",
                findings=[(f, k.value) for f, k in findings],
            )

    async def _store_report(
        self, author: str, key: str, draft: ExampleDraft
    ) -> tuple[ExampleContent, str]:
        saved = await self._artifacts.save(
            KNOWLEDGE_OWNER,
            media_type=MARKDOWN,
            content=draft.report_markdown.encode("utf-8"),
            idempotency_key=hashlib.sha256(f"{author}:{key}".encode()).hexdigest(),
        )
        content = ExampleContent(
            question=draft.question,
            sql=draft.sql,
            method_summary=draft.method_summary,
            report_artifact_id=saved.artifact_id,
            report_artifact_version=saved.version,
        )
        return content, saved.sha256

    # -- review ------------------------------------------------------------

    async def review_queue(
        self, principal: Principal, limit: int = 50
    ) -> Sequence[CandidateSummary]:
        await self._resolver.require_permission(principal, Permission.KNOWLEDGE_REVIEW)
        rows = await self._repo.by_status(ReviewStatus.CANDIDATE, limit)
        return [
            CandidateSummary(
                v.ref,
                v.origin,
                v.author_id,
                v.access.is_shared,
                v.applicability.schema_version,
                v.created_at,
            )
            for v in rows
        ]

    async def read_for_review(
        self, principal: Principal, ref: ExampleRef
    ) -> GoldenExample:
        """Full content for a reviewer; restricted content needs their scope."""
        access = await self._resolver.require_permission(
            principal, Permission.KNOWLEDGE_REVIEW
        )
        version = await self._require(ref)
        if not version.access.permits(access.product_scope):
            raise AccessDenied("example", ref.example_id)
        return await self._example(version)

    async def approve(
        self,
        principal: Principal,
        ref: ExampleRef,
        *,
        rationale: str,
        checks: ApprovalChecks,
    ) -> ChangeResult:
        """Publish a candidate; older versions of the example are retired."""
        if not checks.complete:
            raise KnowledgeError(
                KnowledgeErrorCode.INVALID_REQUEST,
                "publication needs correctness, sanitization and applicability checks",
            )
        return await self._review(
            principal,
            ref,
            ReviewAction.APPROVE,
            rationale,
            {
                "correct": checks.correct,
                "sanitized": checks.sanitized,
                "applicable": checks.applicable,
            },
            supersede=True,
        )

    async def reject(
        self, principal: Principal, ref: ExampleRef, *, rationale: str
    ) -> ChangeResult:
        return await self._review(principal, ref, ReviewAction.REJECT, rationale)

    async def suspend(
        self, principal: Principal, ref: ExampleRef, *, rationale: str
    ) -> ChangeResult:
        """Stop delivery pending investigation; reversible by ``reinstate``."""
        return await self._review(principal, ref, ReviewAction.SUSPEND, rationale)

    async def reinstate(
        self, principal: Principal, ref: ExampleRef, *, rationale: str
    ) -> ChangeResult:
        return await self._review(principal, ref, ReviewAction.REINSTATE, rationale)

    async def retire(
        self, principal: Principal, ref: ExampleRef, *, rationale: str
    ) -> ChangeResult:
        return await self._review(principal, ref, ReviewAction.RETIRE, rationale)

    async def suspend_by_source(
        self,
        principal: Principal,
        source_kind: SourceKind,
        source_id: str,
        *,
        rationale: str,
    ) -> tuple[ExampleRef, ...]:
        """Suspend every published example derived from an incorrect source.

        Ordinary deletion of a source report does not do this; only a reviewer
        who finds the source wrong does.
        """
        done: list[ExampleRef] = []
        for version in await self._by_source(principal, source_kind, source_id):
            if version.status is ReviewStatus.PUBLISHED:
                result = await self.suspend(principal, version.ref, rationale=rationale)
                done.append(result.version.ref)
        return tuple(done)

    async def erase(
        self, principal: Principal, ref: ExampleRef, *, reason: ErasureReason
    ) -> GoldenVersion:
        """Privacy removal: drop content, stored report and index entries.

        Safe to repeat: a crash after the status change leaves a purge that a
        rerun (or ``finish_pending_erasures``) completes.
        """
        access = await self._resolver.require_permission(
            principal, Permission.KNOWLEDGE_REVIEW
        )
        version = await self._require(ref)
        if version.status is not ReviewStatus.ERASED:
            transition = decide(ReviewAction.ERASE, version, access.executive_id)
            version = (
                await self._repo.apply(
                    StatusChange(
                        ref=ref,
                        expected=version.status,
                        action=ReviewAction.ERASE,
                        to_status=transition.to_status,
                        index=transition.index,
                        actor_id=access.executive_id,
                        rationale=reason.value,
                        checks=None,
                        at=self._clock(),
                    )
                )
            ).version
        await self.finish_pending_erasures()
        return version

    async def erase_by_source(
        self,
        principal: Principal,
        source_kind: SourceKind,
        source_id: str,
        *,
        reason: ErasureReason,
    ) -> tuple[ExampleRef, ...]:
        """Erase every version derived from a source, whatever its status."""
        found = await self._by_source(principal, source_kind, source_id)
        for version in found:
            await self.erase(principal, version.ref, reason=reason)
        return tuple(v.ref for v in found)

    async def finish_pending_erasures(self, limit: int = 100) -> int:
        """Purge report artifacts of erased versions; returns how many."""
        pending = await self._repo.pending_purges(limit)
        for ref, artifact_id in pending:
            await self._maintenance.purge(artifact_id)
            await self._repo.purge_done(ref)
        return len(pending)

    async def history(
        self, principal: Principal, ref: ExampleRef
    ) -> Sequence[ReviewEvent]:
        await self._resolver.require_permission(principal, Permission.KNOWLEDGE_REVIEW)
        await self._require(ref)
        return await self._repo.events(ref)

    # -- internals ---------------------------------------------------------

    async def _by_source(
        self, principal: Principal, kind: SourceKind, source_id: str
    ) -> Sequence[GoldenVersion]:
        await self._resolver.require_permission(principal, Permission.KNOWLEDGE_REVIEW)
        if kind is SourceKind.AUTHORED or not OPAQUE_ID_PATTERN.fullmatch(source_id):
            raise KnowledgeError(KnowledgeErrorCode.INVALID_REQUEST, "invalid source")
        return await self._repo.by_source(kind, source_id)

    async def _require(self, ref: ExampleRef) -> GoldenVersion:
        version = await self._repo.get(ref)
        if version is None:
            raise KnowledgeError(KnowledgeErrorCode.NOT_FOUND, "example not found")
        return version

    async def _review(
        self,
        principal: Principal,
        ref: ExampleRef,
        action: ReviewAction,
        rationale: str,
        checks: Mapping[str, bool] | None = None,
        *,
        supersede: bool = False,
    ) -> ChangeResult:
        access = await self._resolver.require_permission(
            principal, Permission.KNOWLEDGE_REVIEW
        )
        text = _clean_rationale(rationale)
        version = await self._require(ref)
        # Judging content (approve/reject/reinstate) needs the right to see it;
        # suspend and retire act without reading it.
        if action in _CONTENT_JUDGEMENTS and not version.access.permits(
            access.product_scope
        ):
            raise AccessDenied("example", ref.example_id)
        try:
            transition = decide(action, version, access.executive_id)
        except SelfReview:
            raise KnowledgeError(
                KnowledgeErrorCode.SELF_REVIEW,
                "an author cannot review their own example",
            ) from None
        return await self._repo.apply(
            StatusChange(
                ref=ref,
                expected=version.status,
                action=action,
                to_status=transition.to_status,
                index=transition.index,
                actor_id=access.executive_id,
                rationale=text,
                checks=checks,
                at=self._clock(),
                supersede_others=supersede,
            )
        )


class GoldenKnowledgeReader(_Store):
    """The delivery gate between stored examples and the model."""

    def __init__(self, repository: KnowledgeRepository, artifacts: ArtifactService):
        super().__init__(artifacts)
        self._repo = repository

    async def deliver(
        self,
        scope: ProductScope,
        context: ApplicabilityContext,
        candidates: Sequence[tuple[ExampleRef, str | None]],
    ) -> Delivery:
        """Deliver those candidates that are eligible *now*.

        ``candidates`` are (reference, content digest the index entry was built
        from). A digest that no longer matches means the entry is stale and the
        example is refused rather than delivered with different content.
        """
        delivered: list[GoldenExample] = []
        refused: list[Refused] = []
        for ref, pinned in candidates:
            version = await self._repo.get(ref)
            reason = (
                DeliveryRefusal.NOT_FOUND
                if version is None
                else version.deliverable_to(scope, context)
            )
            if (
                version is not None
                and reason is None
                and pinned is not None
                and pinned != version.content_digest
            ):
                reason = DeliveryRefusal.STALE_INDEX
            if version is None or reason is not None:
                refused.append(Refused(ref, reason or DeliveryRefusal.NOT_FOUND))
                continue
            try:
                delivered.append(await self._example(version))
            except KnowledgeError:
                refused.append(Refused(ref, DeliveryRefusal.CONTENT_UNAVAILABLE))
        return Delivery(tuple(delivered), tuple(refused))
