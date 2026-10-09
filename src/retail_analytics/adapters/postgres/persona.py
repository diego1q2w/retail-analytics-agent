"""PostgreSQL ``PersonaRepository``.

Locking order, the same in every operation that takes both: the ``persona_state``
row first (publish, rollback; run pinning takes it ``FOR SHARE``), then the
version row. Editing operations lock only the version row and read the active
pointer without a lock, so no two operations can wait on each other. A freeze
trigger (migration 0015) keeps published and discarded versions unchangeable.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import exc
from sqlalchemy.dialects.postgresql import insert as pg_insert

from retail_analytics.adapters.postgres.audit import append_audit
from retail_analytics.adapters.postgres.database import Database
from retail_analytics.adapters.postgres.schema import persona_publications as pp
from retail_analytics.adapters.postgres.schema import persona_state as ps
from retail_analytics.adapters.postgres.schema import persona_versions as pv
from retail_analytics.adapters.postgres.schema import run_personas as rpn
from retail_analytics.application.contracts.audit import AuditEvent
from retail_analytics.application.contracts.persona import (
    NewPersonaDraft,
    PersonaDraftUpdate,
    PublicationRequest,
)
from retail_analytics.domain.persona import (
    COMPANY_PERSONA,
    DRAFT_CREATED,
    DRAFT_DISCARDED,
    DRAFT_UPDATED,
    PREVIEWED,
    PUBLISHED,
    REJECTED,
    ROLLED_BACK,
    FindingKind,
    PersonaError,
    PersonaErrorCode,
    PersonaFinding,
    PersonaVersion,
    Publication,
    PublicationAction,
    Severity,
    VersionState,
    content_digest,
)

_SUBJECT = "persona_version"


def _findings(raw: list[dict[str, str]]) -> tuple[PersonaFinding, ...]:
    return tuple(
        PersonaFinding(FindingKind(item["kind"]), Severity(item["severity"]))
        for item in raw
    )


def _dump(findings: tuple[PersonaFinding, ...]) -> list[dict[str, str]]:
    return [{"kind": f.kind.value, "severity": f.severity.value} for f in findings]


def _version(row: sa.Row[Any]) -> PersonaVersion:
    return PersonaVersion(
        version_id=row.version_id,
        number=row.number,
        content=row.content,
        content_digest=row.content_digest,
        base_version_id=row.base_version_id,
        author_id=row.author_id,
        state=VersionState(row.state),
        revision=row.revision,
        findings=_findings(row.findings),
        previewed_digest=row.previewed_digest,
        created_at=row.created_at,
        updated_at=row.updated_at,
        first_published_at=row.first_published_at,
    )


def _not_found() -> PersonaError:
    return PersonaError(PersonaErrorCode.NOT_FOUND, "No such persona version.")


def _fetch(
    connection: sa.Connection, version_id: str, *, lock: bool = False
) -> PersonaVersion:
    query = sa.select(pv).where(pv.c.version_id == version_id)
    if lock:
        query = query.with_for_update()
    row = connection.execute(query).first()
    if row is None:
        raise _not_found()
    return _version(row)


def _current_id(connection: sa.Connection, *, lock: str | None = None) -> str | None:
    query = sa.select(ps.c.current_version_id).where(ps.c.persona_id == COMPANY_PERSONA)
    if lock == "update":
        query = query.with_for_update()
    elif lock == "share":
        query = query.with_for_update(read=True)
    current: str | None = connection.execute(query).scalar_one()
    return current


def _audit(
    connection: sa.Connection,
    *,
    audit_id: str,
    at: datetime,
    actor_id: str,
    action: str,
    version: PersonaVersion | None,
    subject_id: str,
    extra: dict[str, object] | None = None,
) -> None:
    details: dict[str, object] = {}
    if version is not None:
        details = {
            "number": version.number,
            "revision": version.revision,
            "content_sha256": version.content_digest,
            "base_version_id": version.base_version_id,
            "finding_kinds": sorted({f.kind.value for f in version.findings}),
        }
    details.update(extra or {})
    append_audit(
        connection,
        AuditEvent(
            audit_id=audit_id,
            occurred_at=at,
            actor_id=actor_id,
            action=action,
            subject_type=_SUBJECT,
            subject_id=subject_id,
            details=details,
        ),
    )


class PostgresPersonaRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def current(self) -> PersonaVersion | None:
        return await self._db.transaction(self._current)

    async def get(self, version_id: str) -> PersonaVersion:
        return await self._db.transaction(_fetch, version_id)

    async def create_draft(self, new: NewPersonaDraft) -> tuple[PersonaVersion, bool]:
        return await self._db.transaction(self._create_draft, new)

    async def update_draft(self, update: PersonaDraftUpdate) -> PersonaVersion:
        return await self._db.transaction(self._update_draft, update)

    async def discard_draft(
        self, version_id: str, *, author_id: str, audit_id: str, at: datetime
    ) -> PersonaVersion:
        return await self._db.transaction(
            self._discard, version_id, author_id, audit_id, at
        )

    async def mark_previewed(
        self,
        version_id: str,
        *,
        digest: str,
        actor_id: str,
        audit_id: str,
        at: datetime,
    ) -> PersonaVersion:
        return await self._db.transaction(
            self._mark_previewed, version_id, digest, actor_id, audit_id, at
        )

    async def publish(self, request: PublicationRequest) -> Publication:
        return await self._db.transaction(self._publish, request)

    async def rollback(self, request: PublicationRequest) -> Publication:
        return await self._db.transaction(self._rollback, request)

    async def history(
        self, limit: int
    ) -> tuple[tuple[PersonaVersion, ...], tuple[Publication, ...], str | None]:
        return await self._db.transaction(self._history, limit)

    async def pin_for_run(self, run_id: str, *, at: datetime) -> PersonaVersion | None:
        return await self._db.transaction(self._pin, run_id, at)

    async def record_rejection(
        self,
        *,
        actor_id: str,
        audit_id: str,
        at: datetime,
        action: str,
        subject_id: str,
        reason: str,
    ) -> None:
        await self._db.transaction(
            self._record_rejection, actor_id, audit_id, at, action, subject_id, reason
        )

    # --- transactions -----------------------------------------------------------

    @staticmethod
    def _current(connection: sa.Connection) -> PersonaVersion | None:
        current = _current_id(connection)
        return None if current is None else _fetch(connection, current)

    @staticmethod
    def _create_draft(
        connection: sa.Connection, new: NewPersonaDraft
    ) -> tuple[PersonaVersion, bool]:
        digest = content_digest(new.content)
        inserted = connection.execute(
            pg_insert(pv)
            .values(
                version_id=new.version_id,
                content=new.content,
                content_digest=digest,
                base_version_id=_current_id(connection),
                author_id=new.author_id,
                state=VersionState.DRAFT.value,
                revision=1,
                findings=_dump(new.findings),
                idempotency_key=new.idempotency_key,
                created_at=new.created_at,
                updated_at=new.created_at,
            )
            .on_conflict_do_nothing(constraint="uq_persona_versions_idempotency")
            .returning(pv)
        ).first()
        if inserted is None:
            existing = connection.execute(
                sa.select(pv).where(
                    pv.c.author_id == new.author_id,
                    pv.c.idempotency_key == new.idempotency_key,
                )
            ).one()
            found = _version(existing)
            if found.content_digest != digest and found.revision == 1:
                raise PersonaError(
                    PersonaErrorCode.IDEMPOTENCY_CONFLICT,
                    "This idempotency_key already created a different draft.",
                )
            return found, False
        version = _version(inserted)
        _audit(
            connection,
            audit_id=new.audit_id,
            at=new.created_at,
            actor_id=new.author_id,
            action=DRAFT_CREATED,
            version=version,
            subject_id=version.version_id,
        )
        return version, True

    @staticmethod
    def _own_draft(
        connection: sa.Connection, version_id: str, author_id: str | None
    ) -> PersonaVersion:
        version = _fetch(connection, version_id, lock=True)
        if author_id is not None and version.author_id != author_id:
            # Another editor's draft is indistinguishable from a missing one.
            raise _not_found()
        if version.state is not VersionState.DRAFT:
            raise PersonaError(
                PersonaErrorCode.NOT_A_DRAFT, "Only drafts can be changed."
            )
        return version

    @classmethod
    def _update_draft(
        cls, connection: sa.Connection, update: PersonaDraftUpdate
    ) -> PersonaVersion:
        draft = cls._own_draft(connection, update.version_id, update.author_id)
        if draft.revision != update.expected_revision:
            raise PersonaError(
                PersonaErrorCode.CONFLICT,
                "The draft changed since you read it; fetch it again.",
            )
        row = connection.execute(
            sa.update(pv)
            .where(pv.c.version_id == update.version_id)
            .values(
                content=update.content,
                content_digest=content_digest(update.content),
                findings=_dump(update.findings),
                revision=draft.revision + 1,
                previewed_digest=None,
                # Re-based on whatever is active now; the next preview shows it.
                base_version_id=_current_id(connection),
                updated_at=update.at,
            )
            .returning(pv)
        ).one()
        version = _version(row)
        _audit(
            connection,
            audit_id=update.audit_id,
            at=update.at,
            actor_id=update.author_id,
            action=DRAFT_UPDATED,
            version=version,
            subject_id=version.version_id,
        )
        return version

    @classmethod
    def _discard(
        cls,
        connection: sa.Connection,
        version_id: str,
        author_id: str,
        audit_id: str,
        at: datetime,
    ) -> PersonaVersion:
        cls._own_draft(connection, version_id, author_id)
        row = connection.execute(
            sa.update(pv)
            .where(pv.c.version_id == version_id)
            .values(state=VersionState.DISCARDED.value, updated_at=at)
            .returning(pv)
        ).one()
        version = _version(row)
        _audit(
            connection,
            audit_id=audit_id,
            at=at,
            actor_id=author_id,
            action=DRAFT_DISCARDED,
            version=version,
            subject_id=version_id,
        )
        return version

    @classmethod
    def _mark_previewed(
        cls,
        connection: sa.Connection,
        version_id: str,
        digest: str,
        actor_id: str,
        audit_id: str,
        at: datetime,
    ) -> PersonaVersion:
        draft = cls._own_draft(connection, version_id, None)
        if draft.content_digest != digest:
            raise PersonaError(
                PersonaErrorCode.CONFLICT, "The draft changed while it was previewed."
            )
        row = connection.execute(
            sa.update(pv)
            .where(pv.c.version_id == version_id)
            .values(previewed_digest=digest)
            .returning(pv)
        ).one()
        version = _version(row)
        _audit(
            connection,
            audit_id=audit_id,
            at=at,
            actor_id=actor_id,
            action=PREVIEWED,
            version=version,
            subject_id=version_id,
        )
        return version

    @staticmethod
    def _move_pointer(
        connection: sa.Connection,
        request: PublicationRequest,
        *,
        version: PersonaVersion,
        previous: str | None,
        action: PublicationAction,
        audit_action: str,
    ) -> Publication:
        connection.execute(
            sa.update(ps)
            .where(ps.c.persona_id == COMPANY_PERSONA)
            .values(
                current_version_id=version.version_id,
                publication_seq=ps.c.publication_seq + 1,
            )
        )
        sequence = connection.execute(
            sa.insert(pp)
            .values(
                version_id=version.version_id,
                previous_version_id=previous,
                action=action.value,
                actor_id=request.actor_id,
                published_at=request.at,
            )
            .returning(pp.c.sequence)
        ).scalar_one()
        _audit(
            connection,
            audit_id=request.audit_id,
            at=request.at,
            actor_id=request.actor_id,
            action=audit_action,
            version=version,
            subject_id=version.version_id,
            extra={"previous_version_id": previous, "publication_sequence": sequence},
        )
        return Publication(
            sequence=sequence,
            version_id=version.version_id,
            version_number=version.number,
            previous_version_id=previous,
            action=action,
            actor_id=request.actor_id,
            at=request.at,
        )

    @classmethod
    def _publish(
        cls, connection: sa.Connection, request: PublicationRequest
    ) -> Publication:
        current = _current_id(connection, lock="update")
        draft = _fetch(connection, request.version_id, lock=True)
        if draft.state is not VersionState.DRAFT:
            raise PersonaError(
                PersonaErrorCode.NOT_A_DRAFT, "Only a draft can be published."
            )
        if current != request.expected_current:
            raise PersonaError(
                PersonaErrorCode.CONFLICT,
                "The published persona changed since you looked; preview again.",
            )
        if draft.base_version_id != current:
            raise PersonaError(
                PersonaErrorCode.CONFLICT,
                "Another version was published after this draft was started; "
                "update the draft and preview it again.",
            )
        if not draft.previewed:
            raise PersonaError(
                PersonaErrorCode.NOT_PREVIEWED,
                "Preview this exact text before publishing it.",
            )
        if draft.blocking_findings:
            raise PersonaError(
                PersonaErrorCode.POLICY_CONFLICT,
                "The draft conflicts with fixed policy.",
                findings=draft.blocking_findings,
            )
        row = connection.execute(
            sa.update(pv)
            .where(pv.c.version_id == draft.version_id)
            .values(
                state=VersionState.PUBLISHED.value,
                first_published_at=request.at,
                updated_at=request.at,
            )
            .returning(pv)
        ).one()
        return cls._move_pointer(
            connection,
            request,
            version=_version(row),
            previous=current,
            action=PublicationAction.PUBLISH,
            audit_action=PUBLISHED,
        )

    @classmethod
    def _rollback(
        cls, connection: sa.Connection, request: PublicationRequest
    ) -> Publication:
        current = _current_id(connection, lock="update")
        target = _fetch(connection, request.version_id)
        if target.first_published_at is None:
            raise PersonaError(
                PersonaErrorCode.NOT_PUBLISHED_BEFORE,
                "Only a version that was published before can be rolled back to.",
            )
        if current != request.expected_current:
            raise PersonaError(
                PersonaErrorCode.CONFLICT,
                "The published persona changed since you looked; check again.",
            )
        if current == target.version_id:
            raise PersonaError(
                PersonaErrorCode.CONFLICT, "That version is already active."
            )
        return cls._move_pointer(
            connection,
            request,
            version=target,
            previous=current,
            action=PublicationAction.ROLLBACK,
            audit_action=ROLLED_BACK,
        )

    @staticmethod
    def _history(
        connection: sa.Connection, limit: int
    ) -> tuple[tuple[PersonaVersion, ...], tuple[Publication, ...], str | None]:
        versions = tuple(
            _version(r)
            for r in connection.execute(
                sa.select(pv).order_by(pv.c.number.desc()).limit(limit)
            )
        )
        publications = tuple(
            Publication(
                sequence=r.sequence,
                version_id=r.version_id,
                version_number=r.number,
                previous_version_id=r.previous_version_id,
                action=PublicationAction(r.action),
                actor_id=r.actor_id,
                at=r.published_at,
            )
            for r in connection.execute(
                sa.select(pp, pv.c.number)
                .select_from(pp.join(pv, pv.c.version_id == pp.c.version_id))
                .order_by(pp.c.sequence.desc())
                .limit(limit)
            )
        )
        return versions, publications, _current_id(connection)

    @staticmethod
    def _pin(
        connection: sa.Connection, run_id: str, at: datetime
    ) -> PersonaVersion | None:
        existing = connection.execute(
            sa.select(rpn.c.version_id).where(rpn.c.run_id == run_id)
        ).first()
        if existing is None:
            # FOR SHARE waits for a publication in progress, so the pin is
            # either entirely before or entirely after it.
            current = _current_id(connection, lock="share")
            try:
                with connection.begin_nested():
                    connection.execute(
                        pg_insert(rpn)
                        .values(run_id=run_id, version_id=current, pinned_at=at)
                        .on_conflict_do_nothing(index_elements=[rpn.c.run_id])
                    )
            except exc.IntegrityError:
                raise _not_found() from None
            existing = connection.execute(
                sa.select(rpn.c.version_id).where(rpn.c.run_id == run_id)
            ).one()
        pinned = existing.version_id
        return None if pinned is None else _fetch(connection, pinned)

    @staticmethod
    def _record_rejection(
        connection: sa.Connection,
        actor_id: str,
        audit_id: str,
        at: datetime,
        action: str,
        subject_id: str,
        reason: str,
    ) -> None:
        _audit(
            connection,
            audit_id=audit_id,
            at=at,
            actor_id=actor_id,
            action=action if action else REJECTED,
            version=None,
            subject_id=subject_id,
            extra={"reason": reason},
        )
