"""In-memory ``KnowledgeRepository``/``KnowledgeIndexSource`` for fast tests.

Mirrors the PostgreSQL contract the shared scenarios assert (status check
under "lock", supersede, ordered index events, erase with pending purge).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from retail_analytics.application.authorization import AccessDenied
from retail_analytics.application.contracts.knowledge import (
    ChangeResult,
    IndexChange,
    IndexDocument,
    NewCandidate,
    ReviewEvent,
    StatusChange,
)
from retail_analytics.application.knowledge import (
    KnowledgeError,
    KnowledgeErrorCode,
)
from retail_analytics.domain.knowledge import (
    ExampleRef,
    GoldenVersion,
    IndexChangeKind,
    ReviewAction,
    ReviewStatus,
    SourceKind,
)


class MemoryKnowledgeRepository:
    def __init__(self) -> None:
        self.versions: dict[ExampleRef, GoldenVersion] = {}
        self.keys: dict[tuple[str, str], ExampleRef] = {}
        self.review_events: list[ReviewEvent] = []
        self.index_events: list[IndexChange] = []
        self.purges: dict[ExampleRef, str] = {}

    async def add_candidate(self, c: NewCandidate) -> GoldenVersion:
        if (found := self.keys.get((c.author_id, c.idempotency_key))) is not None:
            return self.versions[found]
        mine = [v for r, v in self.versions.items() if r.example_id == c.example_id]
        if any(v.author_id != c.author_id for v in mine):
            raise AccessDenied("example", c.example_id)
        ref = ExampleRef(c.example_id, len(mine) + 1)
        version = GoldenVersion(
            example_id=c.example_id,
            version=ref.version,
            status=ReviewStatus.CANDIDATE,
            origin=c.origin,
            author_id=c.author_id,
            access=c.access,
            applicability=c.applicability,
            provenance=c.provenance,
            created_at=c.at,
            status_changed_at=c.at,
            content=c.content,
            content_digest=c.content_digest,
        )
        self.versions[ref] = version
        self.keys[(c.author_id, c.idempotency_key)] = ref
        self._audit(ref, ReviewAction.SUBMIT, c.author_id, None, version.status, c)
        return version

    def _audit(
        self,
        ref: ExampleRef,
        action: ReviewAction,
        actor: str,
        old: ReviewStatus | None,
        new: ReviewStatus,
        c: object,
    ) -> None:
        at = getattr(c, "at")  # noqa: B009
        self.review_events.append(
            ReviewEvent(
                ref.example_id,
                ref.version,
                action,
                actor,
                old,
                new,
                "submitted",
                None,
                at,
            )
        )

    def _index(
        self, ref: ExampleRef, kind: IndexChangeKind, reason: ReviewAction, at: object
    ) -> None:
        self.index_events.append(
            IndexChange(
                len(self.index_events) + 1,
                ref.example_id,
                ref.version,
                kind,
                reason,
                at,  # type: ignore[arg-type]
            )
        )

    async def get(self, ref: ExampleRef) -> GoldenVersion | None:
        return self.versions.get(ref)

    async def apply(self, change: StatusChange) -> ChangeResult:
        current = self.versions.get(change.ref)
        if current is None:
            raise KnowledgeError(KnowledgeErrorCode.NOT_FOUND, "example not found")
        if current.status is not change.expected:
            raise KnowledgeError(KnowledgeErrorCode.CONFLICT, "changed")
        superseded: list[ExampleRef] = []
        if change.to_status is ReviewStatus.PUBLISHED:
            live = [
                r
                for r, v in self.versions.items()
                if r.example_id == change.ref.example_id
                and r != change.ref
                and v.status in (ReviewStatus.PUBLISHED, ReviewStatus.SUSPENDED)
            ]
            if live and not change.supersede_others:
                raise KnowledgeError(KnowledgeErrorCode.CONFLICT, "another live")
            for r in live:
                old = self.versions[r]
                self.versions[r] = replace(
                    old, status=ReviewStatus.RETIRED, status_changed_at=change.at
                )
                self.review_events.append(
                    ReviewEvent(
                        r.example_id,
                        r.version,
                        ReviewAction.SUPERSEDE,
                        change.actor_id,
                        old.status,
                        ReviewStatus.RETIRED,
                        "superseded",
                        None,
                        change.at,
                    )
                )
                if old.status is ReviewStatus.PUBLISHED:
                    self._index(
                        r, IndexChangeKind.REMOVE, ReviewAction.SUPERSEDE, change.at
                    )
                superseded.append(r)
        fields: dict[str, object] = {
            "status": change.to_status,
            "status_changed_at": change.at,
        }
        if change.to_status is ReviewStatus.ERASED:
            assert current.content is not None
            self.purges[change.ref] = current.content.report_artifact_id
            fields.update(content=None, content_digest=None)
        elif change.action in (
            ReviewAction.APPROVE,
            ReviewAction.REJECT,
            ReviewAction.REINSTATE,
        ):
            fields.update(reviewed_by=change.actor_id, reviewed_at=change.at)
        updated = replace(current, **fields)  # type: ignore[arg-type]
        self.versions[change.ref] = updated
        self.review_events.append(
            ReviewEvent(
                change.ref.example_id,
                change.ref.version,
                change.action,
                change.actor_id,
                change.expected,
                change.to_status,
                change.rationale,
                change.checks,
                change.at,
            )
        )
        if change.index is not None:
            self._index(change.ref, change.index, change.action, change.at)
        return ChangeResult(updated, tuple(superseded))

    async def by_source(
        self, source_kind: SourceKind, source_id: str
    ) -> Sequence[GoldenVersion]:
        return [
            v
            for v in self.versions.values()
            if v.provenance.source_kind is source_kind
            and v.provenance.source_id == source_id
        ]

    async def by_status(
        self, status: ReviewStatus, limit: int
    ) -> Sequence[GoldenVersion]:
        return [v for v in self.versions.values() if v.status is status][:limit]

    async def events(self, ref: ExampleRef) -> Sequence[ReviewEvent]:
        return [
            e
            for e in self.review_events
            if (e.example_id, e.version) == (ref.example_id, ref.version)
        ]

    async def pending_purges(self, limit: int) -> Sequence[tuple[ExampleRef, str]]:
        return list(self.purges.items())[:limit]

    async def purge_done(self, ref: ExampleRef) -> None:
        self.purges.pop(ref, None)

    async def published_documents(
        self, after: ExampleRef | None, limit: int
    ) -> Sequence[IndexDocument]:
        docs = [
            IndexDocument(
                v.ref,
                v.content_digest,
                v.content.question,
                v.content.method_summary,
                v.access,
                v.applicability,
            )
            for v in self.versions.values()
            if v.status is ReviewStatus.PUBLISHED
            and v.content is not None
            and v.content_digest is not None
        ]
        return docs[:limit]

    async def changes_after(self, sequence: int, limit: int) -> Sequence[IndexChange]:
        return [c for c in self.index_events if c.sequence > sequence][:limit]
