"""Persona management: draft, preview, publish, roll back, deliver to runs.

Flow (design section 21)
------------------------
1. An editor (``persona:edit``, checked against current server-side roles on
   every call) creates a draft from free text. Text with personal data is
   refused; text that tries to override fixed policy is stored with its
   findings but cannot be previewed or published.
2. ``preview`` renders the same sample findings under the active and the
   proposed persona and checks that figures, evidence and limitations
   survived. It changes nothing but the draft's own preview mark.
3. ``publish`` makes the previewed draft active in one transaction. It is
   refused when the draft is not previewed in its current text, was based on a
   version that is no longer active, or the caller's view of the active
   version is stale. Refusals are audited.
4. ``rollback`` makes an earlier published version active again, with the
   same concurrency check.

Each run pins the active version when it starts (``PersonaDelivery``); a
publication affects only runs that start afterwards.

The persona is presentation only. Nothing in it reaches authorization, the
tool catalog, metric definitions or the required disclosures; those stay
enforced in code. Screening is a heuristic that flags attempts, not a proof.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from retail_analytics.application.authorization import AccessResolver
from retail_analytics.application.contracts.authorization import Principal
from retail_analytics.application.contracts.persona import (
    NewPersonaDraft,
    PersonaDraftUpdate,
    PersonaHistory,
    PersonaPreview,
    PublicationRequest,
)
from retail_analytics.application.ports.persona import (
    PersonaRepository,
    PreviewRenderer,
)
from retail_analytics.domain.access import Permission
from retail_analytics.domain.persona import (
    PersonaError,
    PersonaErrorCode,
    PersonaFinding,
    PersonaVersion,
    Publication,
    VersionState,
    check_publishable,
    check_storable,
    clean_content,
    content_digest,
    render_persona_section,
    screen_persona,
)
from retail_analytics.domain.persona_sample import SAMPLE_REPORT

type Clock = Callable[[], datetime]
type IdFactory = Callable[[], str]

MAX_KEY_CHARS = 128
PUBLISH_REJECTED = "persona.publish_rejected"
ROLLBACK_REJECTED = "persona.rollback_rejected"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return uuid.uuid4().hex


def _key(value: str) -> str:
    if not value or len(value) > MAX_KEY_CHARS or not value.isprintable():
        raise PersonaError(
            PersonaErrorCode.INVALID_REQUEST, "idempotency_key is invalid."
        )
    return value


class PersonaService:
    def __init__(
        self,
        repository: PersonaRepository,
        resolver: AccessResolver,
        renderer: PreviewRenderer,
        *,
        clock: Clock = _utc_now,
        new_id: IdFactory = _new_id,
    ) -> None:
        self._repository = repository
        self._resolver = resolver
        self._renderer = renderer
        self._clock = clock
        self._new_id = new_id

    async def current(self, principal: Principal) -> PersonaVersion | None:
        await self._editor(principal)
        return await self._repository.current()

    async def get(self, principal: Principal, version_id: str) -> PersonaVersion:
        await self._editor(principal)
        return await self._repository.get(version_id)

    async def history(self, principal: Principal, *, limit: int = 20) -> PersonaHistory:
        await self._editor(principal)
        versions, publications, current = await self._repository.history(
            max(1, min(limit, 100))
        )
        return PersonaHistory(versions, publications, current)

    def check(self, content: str) -> tuple[PersonaFinding, ...]:
        """Findings for text without storing it (pure; no authority needed
        because nothing is read or written)."""
        return screen_persona(clean_content(content))

    async def create_draft(
        self, principal: Principal, content: str, *, idempotency_key: str
    ) -> PersonaVersion:
        """A draft based on the active version. A retried key returns the
        original draft."""
        await self._editor(principal)
        cleaned = clean_content(content)
        findings = screen_persona(cleaned)
        check_storable(findings)
        draft, _ = await self._repository.create_draft(
            NewPersonaDraft(
                version_id=self._new_id(),
                audit_id=self._new_id(),
                author_id=principal.executive_id,
                content=cleaned,
                findings=findings,
                idempotency_key=_key(idempotency_key),
                created_at=self._clock(),
            )
        )
        return draft

    async def update_draft(
        self,
        principal: Principal,
        draft_id: str,
        content: str,
        *,
        expected_revision: int,
    ) -> PersonaVersion:
        """Replace the text of the author's own draft; clears its preview."""
        await self._editor(principal)
        cleaned = clean_content(content)
        findings = screen_persona(cleaned)
        check_storable(findings)
        return await self._repository.update_draft(
            PersonaDraftUpdate(
                version_id=draft_id,
                audit_id=self._new_id(),
                author_id=principal.executive_id,
                content=cleaned,
                findings=findings,
                expected_revision=expected_revision,
                at=self._clock(),
            )
        )

    async def discard_draft(
        self, principal: Principal, draft_id: str
    ) -> PersonaVersion:
        await self._editor(principal)
        return await self._repository.discard_draft(
            draft_id,
            author_id=principal.executive_id,
            audit_id=self._new_id(),
            at=self._clock(),
        )

    async def preview(self, principal: Principal, draft_id: str) -> PersonaPreview:
        """Current and proposed persona over identical sample findings.

        Nothing is published and no run is touched. Refused for text that
        conflicts with fixed policy. The draft is marked previewed only when
        every figure, evidence reference and limitation survived the proposed
        rendering.
        """
        await self._editor(principal)
        draft = await self._repository.get(draft_id)
        if draft.state is not VersionState.DRAFT:
            raise PersonaError(
                PersonaErrorCode.NOT_A_DRAFT, "Only drafts are previewed."
            )
        findings = screen_persona(draft.content)
        check_publishable(findings)
        active = await self._repository.current()
        proposed_section = render_persona_section(draft)
        current_section = None if active is None else render_persona_section(active)
        sample_current = await self._renderer.render(current_section, SAMPLE_REPORT)
        sample_proposed = await self._renderer.render(proposed_section, SAMPLE_REPORT)
        missing_current = SAMPLE_REPORT.missing_from(sample_current)
        missing_proposed = SAMPLE_REPORT.missing_from(sample_proposed)
        if not missing_proposed:
            await self._repository.mark_previewed(
                draft.version_id,
                digest=content_digest(draft.content),
                actor_id=principal.executive_id,
                audit_id=self._new_id(),
                at=self._clock(),
            )
        return PersonaPreview(
            draft_id=draft.version_id,
            draft_number=draft.number,
            current_version_id=None if active is None else active.version_id,
            current_number=None if active is None else active.number,
            findings=findings,
            current_instructions=current_section,
            proposed_instructions=proposed_section,
            sample_current=sample_current,
            sample_proposed=sample_proposed,
            missing_current=missing_current,
            missing_proposed=missing_proposed,
            persona_applied=self._renderer.applies_persona,
        )

    async def publish(
        self, principal: Principal, draft_id: str, *, expected_current: str | None
    ) -> Publication:
        """Make the previewed draft the active persona, atomically.

        ``expected_current`` is the active version id the editor saw in the
        preview (None when there was none). Raises ``PersonaError``.
        """
        await self._editor(principal)
        draft = await self._repository.get(draft_id)
        request = self._request(principal, draft_id, expected_current)
        try:
            if draft.state is VersionState.DRAFT:
                check_publishable(screen_persona(draft.content))
            return await self._repository.publish(request)
        except PersonaError as error:
            await self._audit_rejection(request, PUBLISH_REJECTED, error)
            raise

    async def rollback(
        self, principal: Principal, version_id: str, *, expected_current: str | None
    ) -> Publication:
        """Make an earlier published version active again, atomically."""
        await self._editor(principal)
        request = self._request(principal, version_id, expected_current)
        try:
            return await self._repository.rollback(request)
        except PersonaError as error:
            await self._audit_rejection(request, ROLLBACK_REJECTED, error)
            raise

    def _request(
        self, principal: Principal, version_id: str, expected_current: str | None
    ) -> PublicationRequest:
        return PublicationRequest(
            version_id=version_id,
            expected_current=expected_current,
            actor_id=principal.executive_id,
            audit_id=self._new_id(),
            at=self._clock(),
        )

    async def _audit_rejection(
        self, request: PublicationRequest, action: str, error: PersonaError
    ) -> None:
        if error.code is PersonaErrorCode.NOT_FOUND:
            return
        await self._repository.record_rejection(
            actor_id=request.actor_id,
            audit_id=self._new_id(),
            at=request.at,
            action=action,
            subject_id=request.version_id,
            reason=error.code.value,
        )

    async def _editor(self, principal: Principal) -> None:
        await self._resolver.require_permission(principal, Permission.PERSONA_EDIT)


class PersonaDelivery:
    """What a run uses: its pinned persona as a block of model instructions."""

    def __init__(
        self, repository: PersonaRepository, *, clock: Clock = _utc_now
    ) -> None:
        self._repository = repository
        self._clock = clock

    async def section_for_run(self, run_id: str) -> str | None:
        """Pin the active version on first use; the same text on every later
        call, whatever is published meanwhile. None when no persona exists."""
        version = await self._repository.pin_for_run(run_id, at=self._clock())
        return None if version is None else render_persona_section(version)
