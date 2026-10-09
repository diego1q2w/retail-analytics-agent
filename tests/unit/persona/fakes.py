"""In-memory persona repository: just enough behaviour to test the service.

The real locking, atomicity and freeze rules are tested on PostgreSQL in
``tests/integration/test_persona.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from retail_analytics.application.authorization import AccessResolver, OwnershipGuard
from retail_analytics.application.contracts.authorization import Principal
from retail_analytics.application.contracts.persona import (
    NewPersonaDraft,
    PersonaDraftUpdate,
    PublicationRequest,
)
from retail_analytics.domain.access import ExecutiveAccess, Permission, Role
from retail_analytics.domain.persona import (
    PersonaError,
    PersonaErrorCode,
    PersonaVersion,
    Publication,
    PublicationAction,
    VersionState,
    content_digest,
)

NOW = datetime(2026, 10, 9, 12, tzinfo=UTC)
SCOPES = frozenset(p.value for p in Permission)
EDITOR = Principal("editor-1", SCOPES)
EDITOR_2 = Principal("editor-2", SCOPES)
READER = Principal("reader-1", SCOPES)


@dataclass
class Directory:
    by_id: dict[str, ExecutiveAccess] = field(default_factory=dict)

    async def get(self, executive_id: str) -> ExecutiveAccess | None:
        return self.by_id.get(executive_id)


def resolver() -> AccessResolver:
    def access(who: str, *roles: Role) -> ExecutiveAccess:
        return ExecutiveAccess(who, frozenset(roles), frozenset({"1"}), True, 1)

    directory = Directory(
        {
            "editor-1": access("editor-1", Role.EXECUTIVE, Role.EDITOR),
            "editor-2": access("editor-2", Role.EDITOR),
            "reader-1": access("reader-1", Role.EXECUTIVE, Role.REVIEWER, Role.ADMIN),
        }
    )
    return AccessResolver(directory, OwnershipGuard(None, None, None))  # type: ignore[arg-type]


@dataclass
class MemoryPersona:
    versions: dict[str, PersonaVersion] = field(default_factory=dict)
    active: str | None = None
    publications: list[Publication] = field(default_factory=list)
    rejections: list[tuple[str, str, str]] = field(default_factory=list)
    pins: dict[str, str | None] = field(default_factory=dict)
    next_number: int = 1

    async def current(self) -> PersonaVersion | None:
        return None if self.active is None else self.versions[self.active]

    async def get(self, version_id: str) -> PersonaVersion:
        if version_id not in self.versions:
            raise PersonaError(PersonaErrorCode.NOT_FOUND, "No such persona version.")
        return self.versions[version_id]

    async def create_draft(self, new: NewPersonaDraft) -> tuple[PersonaVersion, bool]:
        version = PersonaVersion(
            version_id=new.version_id,
            number=self.next_number,
            content=new.content,
            content_digest=content_digest(new.content),
            base_version_id=self.active,
            author_id=new.author_id,
            state=VersionState.DRAFT,
            revision=1,
            findings=new.findings,
            previewed_digest=None,
            created_at=new.created_at,
            updated_at=new.created_at,
        )
        self.next_number += 1
        self.versions[version.version_id] = version
        return version, True

    async def update_draft(self, update: PersonaDraftUpdate) -> PersonaVersion:
        found = await self.get(update.version_id)
        if found.author_id != update.author_id:
            raise PersonaError(PersonaErrorCode.NOT_FOUND, "No such persona version.")
        changed = replace(
            found,
            content=update.content,
            content_digest=content_digest(update.content),
            findings=update.findings,
            revision=found.revision + 1,
            previewed_digest=None,
        )
        self.versions[found.version_id] = changed
        return changed

    async def discard_draft(self, version_id: str, **_: object) -> PersonaVersion:
        found = await self.get(version_id)
        changed = replace(found, state=VersionState.DISCARDED)
        self.versions[version_id] = changed
        return changed

    async def mark_previewed(
        self, version_id: str, *, digest: str, **_: object
    ) -> PersonaVersion:
        found = await self.get(version_id)
        changed = replace(found, previewed_digest=digest)
        self.versions[version_id] = changed
        return changed

    async def publish(self, request: PublicationRequest) -> Publication:
        draft = await self.get(request.version_id)
        if request.expected_current != self.active:
            raise PersonaError(PersonaErrorCode.CONFLICT, "moved")
        if not draft.previewed:
            raise PersonaError(PersonaErrorCode.NOT_PREVIEWED, "preview first")
        self.versions[draft.version_id] = replace(
            draft, state=VersionState.PUBLISHED, first_published_at=request.at
        )
        return self._move(request, draft, PublicationAction.PUBLISH)

    async def rollback(self, request: PublicationRequest) -> Publication:
        target = await self.get(request.version_id)
        if target.first_published_at is None:
            raise PersonaError(PersonaErrorCode.NOT_PUBLISHED_BEFORE, "never")
        if request.expected_current != self.active:
            raise PersonaError(PersonaErrorCode.CONFLICT, "moved")
        return self._move(request, target, PublicationAction.ROLLBACK)

    def _move(
        self,
        request: PublicationRequest,
        version: PersonaVersion,
        action: PublicationAction,
    ) -> Publication:
        publication = Publication(
            len(self.publications) + 1,
            version.version_id,
            version.number,
            self.active,
            action,
            request.actor_id,
            request.at,
        )
        self.active = version.version_id
        self.publications.append(publication)
        return publication

    async def history(
        self, limit: int
    ) -> tuple[tuple[PersonaVersion, ...], tuple[Publication, ...], str | None]:
        return tuple(self.versions.values()), tuple(self.publications), self.active

    async def pin_for_run(self, run_id: str, *, at: datetime) -> PersonaVersion | None:
        self.pins.setdefault(run_id, self.active)
        pinned = self.pins[run_id]
        return None if pinned is None else self.versions[pinned]

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
        self.rejections.append((action, subject_id, reason))
