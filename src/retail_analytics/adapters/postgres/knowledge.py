"""PostgreSQL Golden Knowledge repository and index feed.

Every status change runs in one transaction: lock the version row, check the
expected status, update, append the audit event and the index events. A
transaction-level advisory lock around event writes makes event sequence order
equal commit order, so a consumer that remembers the last sequence it saw
cannot skip an event that commits late.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from retail_analytics.adapters.postgres.database import Database
from retail_analytics.adapters.postgres.schema import golden_embeddings as ge
from retail_analytics.adapters.postgres.schema import (
    golden_index_events as ie,
)
from retail_analytics.adapters.postgres.schema import (
    golden_provenance as gp,
)
from retail_analytics.adapters.postgres.schema import (
    golden_review_events as re_,
)
from retail_analytics.adapters.postgres.schema import (
    golden_versions as gv,
)
from retail_analytics.application.authorization import AccessDenied
from retail_analytics.application.knowledge import (
    ChangeResult,
    IndexChange,
    IndexDocument,
    KnowledgeError,
    KnowledgeErrorCode,
    NewCandidate,
    ReviewEvent,
    StatusChange,
)
from retail_analytics.domain.knowledge import (
    Applicability,
    ExampleContent,
    ExampleRef,
    GoldenVersion,
    IndexChangeKind,
    KnowledgeAccess,
    MetricRef,
    Origin,
    Provenance,
    ReviewAction,
    ReviewStatus,
    SourceKind,
)

_EVENT_LOCK = 7_302_023  # arbitrary constant key for the index-event writers
_LIVE = (ReviewStatus.PUBLISHED.value, ReviewStatus.SUSPENDED.value)


def _row_to_version(m: Mapping[Any, Any]) -> GoldenVersion:
    erased = m["question"] is None
    content = (
        None
        if erased
        else ExampleContent(
            question=m["question"],
            sql=m["sql_text"],
            method_summary=m["method_summary"],
            report_artifact_id=m["report_artifact_id"],
            report_artifact_version=m["report_artifact_version"],
        )
    )
    return GoldenVersion(
        example_id=m["example_id"],
        version=m["version"],
        status=ReviewStatus(m["status"]),
        origin=Origin(m["origin"]),
        author_id=m["author_id"],
        access=KnowledgeAccess(frozenset(m["restricted_product_ids"])),
        applicability=Applicability(
            m["schema_version"],
            frozenset(
                MetricRef(r["metric_id"], r["version"]) for r in m["metric_refs"]
            ),
        ),
        provenance=Provenance(
            SourceKind(m["source_kind"]), m["source_id"], m["source_version"]
        ),
        created_at=m["created_at"],
        status_changed_at=m["status_changed_at"],
        content=content,
        content_digest=m["content_digest"],
        reviewed_by=m["reviewed_by"],
        reviewed_at=m["reviewed_at"],
    )


_JOINED = gv.join(
    gp,
    sa.and_(gv.c.example_id == gp.c.example_id, gv.c.version == gp.c.version),
)


def _select() -> sa.Select[Any]:
    return sa.select(
        *gv.c, gp.c.source_kind, gp.c.source_id, gp.c.source_version
    ).select_from(_JOINED)


def _lock_events(connection: sa.Connection) -> None:
    connection.execute(sa.select(sa.func.pg_advisory_xact_lock(_EVENT_LOCK)))


class PostgresKnowledgeRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    # -- KnowledgeRepository ----------------------------------------------

    async def add_candidate(self, candidate: NewCandidate) -> GoldenVersion:
        return await self._db.transaction(self._add, candidate)

    async def get(self, ref: ExampleRef) -> GoldenVersion | None:
        return await self._db.transaction(self._get, ref)

    async def apply(self, change: StatusChange) -> ChangeResult:
        return await self._db.transaction(self._apply, change)

    async def by_source(
        self, source_kind: SourceKind, source_id: str
    ) -> Sequence[GoldenVersion]:
        return await self._db.transaction(self._by_source, source_kind, source_id)

    async def by_status(
        self, status: ReviewStatus, limit: int
    ) -> Sequence[GoldenVersion]:
        return await self._db.transaction(self._by_status, status, limit)

    async def events(self, ref: ExampleRef) -> Sequence[ReviewEvent]:
        return await self._db.transaction(self._events, ref)

    async def pending_purges(self, limit: int) -> Sequence[tuple[ExampleRef, str]]:
        return await self._db.transaction(self._pending, limit)

    async def purge_done(self, ref: ExampleRef) -> None:
        await self._db.transaction(self._purge_done, ref)

    # -- KnowledgeIndexSource ---------------------------------------------

    async def published_documents(
        self, after: ExampleRef | None, limit: int
    ) -> Sequence[IndexDocument]:
        return await self._db.transaction(self._documents, after, limit)

    async def changes_after(self, sequence: int, limit: int) -> Sequence[IndexChange]:
        return await self._db.transaction(self._changes, sequence, limit)

    # -- transactions -------------------------------------------------------

    def _add(self, connection: sa.Connection, c: NewCandidate) -> GoldenVersion:
        connection.execute(
            sa.select(
                sa.func.pg_advisory_xact_lock(sa.func.hashtextextended(c.example_id, 1))
            )
        )
        existing = connection.execute(
            _select().where(
                gv.c.author_id == c.author_id,
                gv.c.idempotency_key == c.idempotency_key,
            )
        ).one_or_none()
        if existing is not None:
            return _row_to_version(existing._mapping)
        current = connection.execute(
            sa.select(gv.c.author_id, sa.func.max(gv.c.version))
            .where(gv.c.example_id == c.example_id)
            .group_by(gv.c.author_id)
        ).all()
        if any(row[0] != c.author_id for row in current):
            raise AccessDenied("example", c.example_id)
        version = max((row[1] for row in current), default=0) + 1
        connection.execute(
            insert(gv).values(
                example_id=c.example_id,
                version=version,
                status=ReviewStatus.CANDIDATE.value,
                origin=c.origin.value,
                author_id=c.author_id,
                idempotency_key=c.idempotency_key,
                restricted_product_ids=sorted(c.access.restricted_product_ids),
                schema_version=c.applicability.schema_version,
                metric_refs=[
                    {"metric_id": m.metric_id, "version": m.version}
                    for m in sorted(
                        c.applicability.metrics, key=lambda m: (m.metric_id, m.version)
                    )
                ],
                question=c.content.question,
                sql_text=c.content.sql,
                method_summary=c.content.method_summary,
                report_artifact_id=c.content.report_artifact_id,
                report_artifact_version=c.content.report_artifact_version,
                content_digest=c.content_digest,
                created_at=c.at,
                status_changed_at=c.at,
            )
        )
        connection.execute(
            insert(gp).values(
                example_id=c.example_id,
                version=version,
                source_kind=c.provenance.source_kind.value,
                source_id=c.provenance.source_id,
                source_version=c.provenance.source_version,
            )
        )
        self._event(
            connection,
            ExampleRef(c.example_id, version),
            ReviewAction.SUBMIT,
            c.author_id,
            None,
            ReviewStatus.CANDIDATE,
            "submitted",
            None,
            c.at,
        )
        return self._get(connection, ExampleRef(c.example_id, version))  # type: ignore[return-value]

    @staticmethod
    def _get(connection: sa.Connection, ref: ExampleRef) -> GoldenVersion | None:
        row = connection.execute(
            _select().where(
                gv.c.example_id == ref.example_id, gv.c.version == ref.version
            )
        ).one_or_none()
        return None if row is None else _row_to_version(row._mapping)

    @staticmethod
    def _event(
        connection: sa.Connection,
        ref: ExampleRef,
        action: ReviewAction,
        actor: str,
        from_status: ReviewStatus | None,
        to_status: ReviewStatus,
        rationale: str,
        checks: Mapping[str, bool] | None,
        at: Any,
    ) -> None:
        connection.execute(
            insert(re_).values(
                example_id=ref.example_id,
                version=ref.version,
                action=action.value,
                actor_id=actor,
                from_status=None if from_status is None else from_status.value,
                to_status=to_status.value,
                rationale=rationale,
                checks=None if checks is None else dict(checks),
                at=at,
            )
        )

    @staticmethod
    def _index(
        connection: sa.Connection,
        ref: ExampleRef,
        kind: IndexChangeKind,
        reason: ReviewAction,
        at: Any,
    ) -> None:
        connection.execute(
            insert(ie).values(
                example_id=ref.example_id,
                version=ref.version,
                kind=kind.value,
                reason=reason.value,
                at=at,
            )
        )

    def _apply(self, connection: sa.Connection, change: StatusChange) -> ChangeResult:
        _lock_events(connection)
        ref = change.ref
        row = connection.execute(
            sa.select(gv.c.status, gv.c.report_artifact_id)
            .where(gv.c.example_id == ref.example_id, gv.c.version == ref.version)
            .with_for_update()
        ).one_or_none()
        if row is None:
            raise KnowledgeError(KnowledgeErrorCode.NOT_FOUND, "example not found")
        if row[0] != change.expected.value:
            raise KnowledgeError(
                KnowledgeErrorCode.CONFLICT, "example changed; reload and retry"
            )
        superseded: list[ExampleRef] = []
        if change.to_status is ReviewStatus.PUBLISHED:
            others = connection.execute(
                sa.select(gv.c.version, gv.c.status)
                .where(
                    gv.c.example_id == ref.example_id,
                    gv.c.version != ref.version,
                    gv.c.status.in_(_LIVE),
                )
                .with_for_update()
            ).all()
            if others and not change.supersede_others:
                raise KnowledgeError(
                    KnowledgeErrorCode.CONFLICT,
                    "another version of this example is live",
                )
            for other_version, other_status in others:
                other = ExampleRef(ref.example_id, other_version)
                self._set_status(
                    connection, other, ReviewStatus.RETIRED, change, erase=False
                )
                self._event(
                    connection,
                    other,
                    ReviewAction.SUPERSEDE,
                    change.actor_id,
                    ReviewStatus(other_status),
                    ReviewStatus.RETIRED,
                    f"superseded by version {ref.version}",
                    None,
                    change.at,
                )
                if other_status == ReviewStatus.PUBLISHED.value:
                    self._index(
                        connection,
                        other,
                        IndexChangeKind.REMOVE,
                        ReviewAction.SUPERSEDE,
                        change.at,
                    )
                superseded.append(other)
        erase = change.to_status is ReviewStatus.ERASED
        self._set_status(
            connection, ref, change.to_status, change, erase=erase, purge=row[1]
        )
        self._event(
            connection,
            ref,
            change.action,
            change.actor_id,
            change.expected,
            change.to_status,
            change.rationale,
            change.checks,
            change.at,
        )
        if change.index is not None:
            self._index(connection, ref, change.index, change.action, change.at)
        updated = self._get(connection, ref)
        assert updated is not None  # noqa: S101
        return ChangeResult(updated, tuple(superseded))

    @staticmethod
    def _set_status(
        connection: sa.Connection,
        ref: ExampleRef,
        status: ReviewStatus,
        change: StatusChange,
        *,
        erase: bool,
        purge: str | None = None,
    ) -> None:
        values: dict[str, Any] = {
            "status": status.value,
            "status_changed_at": change.at,
        }
        if (
            change.action
            in (
                ReviewAction.APPROVE,
                ReviewAction.REJECT,
                ReviewAction.REINSTATE,
            )
            and status is not ReviewStatus.RETIRED
        ):
            values["reviewed_by"] = change.actor_id
            values["reviewed_at"] = change.at
        if erase:
            values.update(
                question=None,
                sql_text=None,
                method_summary=None,
                report_artifact_id=None,
                report_artifact_version=None,
                content_digest=None,
                purge_artifact_id=purge,
            )
        connection.execute(
            sa.update(gv)
            .where(gv.c.example_id == ref.example_id, gv.c.version == ref.version)
            .values(**values)
        )
        if erase:
            # Embeddings are keyed by content digest only; drop every vector
            # whose content no remaining version holds (shared content stays).
            connection.execute(
                sa.delete(ge).where(
                    ge.c.content_digest.not_in(
                        sa.select(gv.c.content_digest).where(
                            gv.c.content_digest.is_not(None)
                        )
                    )
                )
            )

    @staticmethod
    def _by_source(
        connection: sa.Connection, kind: SourceKind, source_id: str
    ) -> list[GoldenVersion]:
        rows = connection.execute(
            _select()
            .where(gp.c.source_kind == kind.value, gp.c.source_id == source_id)
            .order_by(gv.c.example_id, gv.c.version)
        ).all()
        return [_row_to_version(r._mapping) for r in rows]

    @staticmethod
    def _by_status(
        connection: sa.Connection, status: ReviewStatus, limit: int
    ) -> list[GoldenVersion]:
        rows = connection.execute(
            _select()
            .where(gv.c.status == status.value)
            .order_by(gv.c.created_at, gv.c.example_id, gv.c.version)
            .limit(limit)
        ).all()
        return [_row_to_version(r._mapping) for r in rows]

    @staticmethod
    def _events(connection: sa.Connection, ref: ExampleRef) -> list[ReviewEvent]:
        rows = connection.execute(
            sa.select(re_)
            .where(re_.c.example_id == ref.example_id, re_.c.version == ref.version)
            .order_by(re_.c.event_id)
        ).all()
        return [
            ReviewEvent(
                example_id=r.example_id,
                version=r.version,
                action=ReviewAction(r.action),
                actor_id=r.actor_id,
                from_status=None
                if r.from_status is None
                else ReviewStatus(r.from_status),
                to_status=ReviewStatus(r.to_status),
                rationale=r.rationale,
                checks=r.checks,
                at=r.at,
            )
            for r in rows
        ]

    @staticmethod
    def _pending(connection: sa.Connection, limit: int) -> list[tuple[ExampleRef, str]]:
        rows = connection.execute(
            sa.select(gv.c.example_id, gv.c.version, gv.c.purge_artifact_id)
            .where(gv.c.purge_artifact_id.is_not(None))
            .order_by(gv.c.example_id, gv.c.version)
            .limit(limit)
        ).all()
        return [(ExampleRef(r[0], r[1]), str(r[2])) for r in rows]

    @staticmethod
    def _purge_done(connection: sa.Connection, ref: ExampleRef) -> None:
        connection.execute(
            sa.update(gv)
            .where(gv.c.example_id == ref.example_id, gv.c.version == ref.version)
            .values(purge_artifact_id=None)
        )

    @staticmethod
    def _documents(
        connection: sa.Connection, after: ExampleRef | None, limit: int
    ) -> list[IndexDocument]:
        query = _select().where(gv.c.status == ReviewStatus.PUBLISHED.value)
        if after is not None:
            query = query.where(
                sa.tuple_(gv.c.example_id, gv.c.version)
                > sa.tuple_(after.example_id, after.version)
            )
        rows = connection.execute(
            query.order_by(gv.c.example_id, gv.c.version).limit(limit)
        ).all()
        documents: list[IndexDocument] = []
        for row in rows:
            v = _row_to_version(row._mapping)
            if v.content is None or v.content_digest is None:
                continue
            documents.append(
                IndexDocument(
                    ref=v.ref,
                    content_digest=v.content_digest,
                    question=v.content.question,
                    method_summary=v.content.method_summary,
                    access=v.access,
                    applicability=v.applicability,
                )
            )
        return documents

    @staticmethod
    def _changes(
        connection: sa.Connection, sequence: int, limit: int
    ) -> list[IndexChange]:
        rows = connection.execute(
            sa.select(ie)
            .where(ie.c.sequence > sequence)
            .order_by(ie.c.sequence)
            .limit(limit)
        ).all()
        return [
            IndexChange(
                sequence=r.sequence,
                example_id=r.example_id,
                version=r.version,
                kind=IndexChangeKind(r.kind),
                reason=ReviewAction(r.reason),
                at=r.at,
            )
            for r in rows
        ]
