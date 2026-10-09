"""Persona management routes: ``/v1/persona``. Editors only (``persona:edit``).

Every call authenticates the bearer token, then the application checks the
caller's current server-side roles; there is no body field that names a caller
or grants anything. Responses carry rule names for flagged text, never the
matched text. Publication and rollback need ``expected_current_version_id``:
the active version the editor looked at, so a concurrent change is refused
with 409 instead of overwritten.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from retail_analytics.application.contracts import Identifier
from retail_analytics.application.contracts.persona import (
    PersonaHistory,
    PersonaPreview,
)
from retail_analytics.domain.persona import (
    MAX_PERSONA_CHARS,
    PersonaFinding,
    PersonaVersion,
    Publication,
)
from retail_analytics.interfaces.http.dependencies import CurrentPrincipal, Services
from retail_analytics.interfaces.http.errors import ApiError
from retail_analytics.interfaces.http.services import HttpServices, Personas

PersonaText = Annotated[
    str, StringConstraints(min_length=1, max_length=MAX_PERSONA_CHARS * 2)
]
SubmissionKey = Annotated[str, StringConstraints(min_length=1, max_length=128)]


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _Response(BaseModel):
    model_config = ConfigDict(frozen=True)


class CreateDraftRequest(_Request):
    content: PersonaText
    submission_key: SubmissionKey


class UpdateDraftRequest(_Request):
    content: PersonaText
    expected_revision: int = Field(ge=1)


class PublishRequest(_Request):
    # The active version the editor saw (null when none was published).
    expected_current_version_id: Identifier | None


class RollbackRequest(_Request):
    target_version_id: Identifier
    expected_current_version_id: Identifier | None


class FindingOut(_Response):
    kind: str
    severity: str

    @classmethod
    def of(cls, finding: PersonaFinding) -> FindingOut:
        return cls(kind=finding.kind.value, severity=finding.severity.value)


class VersionOut(_Response):
    version_id: str
    number: int
    state: str
    content: str
    content_sha256: str
    base_version_id: str | None
    author_id: str
    revision: int
    previewed: bool
    findings: list[FindingOut]
    created_at: datetime
    updated_at: datetime
    first_published_at: datetime | None

    @classmethod
    def of(cls, v: PersonaVersion) -> VersionOut:
        return cls(
            version_id=v.version_id,
            number=v.number,
            state=v.state.value,
            content=v.content,
            content_sha256=v.content_digest,
            base_version_id=v.base_version_id,
            author_id=v.author_id,
            revision=v.revision,
            previewed=v.previewed,
            findings=[FindingOut.of(f) for f in v.findings],
            created_at=v.created_at,
            updated_at=v.updated_at,
            first_published_at=v.first_published_at,
        )


class CurrentOut(_Response):
    current: VersionOut | None


class PublicationOut(_Response):
    sequence: int
    version_id: str
    version_number: int
    previous_version_id: str | None
    action: str
    actor_id: str
    at: datetime

    @classmethod
    def of(cls, p: Publication) -> PublicationOut:
        return cls(
            sequence=p.sequence,
            version_id=p.version_id,
            version_number=p.version_number,
            previous_version_id=p.previous_version_id,
            action=p.action.value,
            actor_id=p.actor_id,
            at=p.at,
        )


class HistoryOut(_Response):
    current_version_id: str | None
    versions: list[VersionOut]
    publications: list[PublicationOut]

    @classmethod
    def of(cls, h: PersonaHistory) -> HistoryOut:
        return cls(
            current_version_id=h.current_version_id,
            versions=[VersionOut.of(v) for v in h.versions],
            publications=[PublicationOut.of(p) for p in h.publications],
        )


class PreviewOut(_Response):
    draft_id: str
    draft_number: int
    current_version_id: str | None
    current_number: int | None
    findings: list[FindingOut]
    current_instructions: str | None
    proposed_instructions: str
    sample_current: str
    sample_proposed: str
    missing_current: list[str]
    missing_proposed: list[str]
    persona_applied: bool
    preserved: bool

    @classmethod
    def of(cls, p: PersonaPreview) -> PreviewOut:
        return cls(
            draft_id=p.draft_id,
            draft_number=p.draft_number,
            current_version_id=p.current_version_id,
            current_number=p.current_number,
            findings=[FindingOut.of(f) for f in p.findings],
            current_instructions=p.current_instructions,
            proposed_instructions=p.proposed_instructions,
            sample_current=p.sample_current,
            sample_proposed=p.sample_proposed,
            missing_current=list(p.missing_current),
            missing_proposed=list(p.missing_proposed),
            persona_applied=p.persona_applied,
            preserved=p.preserved,
        )


def build_persona_router() -> APIRouter:
    router = APIRouter(prefix="/persona", tags=["persona"])

    @router.get("")
    async def current(principal: CurrentPrincipal, s: Services) -> CurrentOut:
        """The active company persona (null when none is published)."""
        found = await _personas(s).current(principal)
        return CurrentOut(current=None if found is None else VersionOut.of(found))

    @router.get("/history")
    async def history(
        principal: CurrentPrincipal, s: Services, limit: int = 20
    ) -> HistoryOut:
        """Versions (newest first) and every publish/rollback."""
        return HistoryOut.of(await _personas(s).history(principal, limit=limit))

    @router.post("/drafts", status_code=201)
    async def create_draft(
        body: CreateDraftRequest, principal: CurrentPrincipal, s: Services
    ) -> VersionOut:
        """A draft based on the active version. Repeating ``submission_key``
        returns the same draft. Personal data is refused; text that conflicts
        with fixed policy is stored with findings but cannot be published."""
        draft = await _personas(s).create_draft(
            principal, body.content, idempotency_key=body.submission_key
        )
        return VersionOut.of(draft)

    @router.get("/versions/{version_id}")
    async def read_version(
        version_id: str, principal: CurrentPrincipal, s: Services
    ) -> VersionOut:
        return VersionOut.of(await _personas(s).get(principal, version_id))

    @router.put("/drafts/{draft_id}")
    async def update_draft(
        draft_id: str,
        body: UpdateDraftRequest,
        principal: CurrentPrincipal,
        s: Services,
    ) -> VersionOut:
        """Replace your own draft's text (409 if ``expected_revision`` is stale)."""
        return VersionOut.of(
            await _personas(s).update_draft(
                principal,
                draft_id,
                body.content,
                expected_revision=body.expected_revision,
            )
        )

    @router.delete("/drafts/{draft_id}", status_code=204)
    async def discard_draft(
        draft_id: str, principal: CurrentPrincipal, s: Services
    ) -> Response:
        await _personas(s).discard_draft(principal, draft_id)
        return Response(status_code=204)

    @router.post("/drafts/{draft_id}/preview")
    async def preview(
        draft_id: str, principal: CurrentPrincipal, s: Services
    ) -> PreviewOut:
        """Current and proposed persona over the same sample findings. Changes
        nothing for other users or running investigations."""
        return PreviewOut.of(await _personas(s).preview(principal, draft_id))

    @router.post("/drafts/{draft_id}/publish")
    async def publish(
        draft_id: str,
        body: PublishRequest,
        principal: CurrentPrincipal,
        s: Services,
    ) -> PublicationOut:
        """Make the previewed draft active; only runs that start afterwards
        use it."""
        return PublicationOut.of(
            await _personas(s).publish(
                principal,
                draft_id,
                expected_current=body.expected_current_version_id,
            )
        )

    @router.post("/rollback")
    async def rollback(
        body: RollbackRequest, principal: CurrentPrincipal, s: Services
    ) -> PublicationOut:
        """Make an earlier published version active again."""
        return PublicationOut.of(
            await _personas(s).rollback(
                principal,
                body.target_version_id,
                expected_current=body.expected_current_version_id,
            )
        )

    return router


def _personas(s: HttpServices) -> Personas:
    if s.persona is None:
        raise ApiError(503, "unavailable", "Persona management is not enabled.")
    return s.persona
