from __future__ import annotations

from datetime import datetime
from typing import Protocol

from retail_analytics.application.contracts.persona import (
    NewPersonaDraft,
    PersonaDraftUpdate,
    PublicationRequest,
)
from retail_analytics.domain.persona import PersonaVersion, Publication
from retail_analytics.domain.persona_sample import SampleReport


class PersonaRepository(Protocol):
    """Persona versions, the active pointer and its history (PostgreSQL).

    Every state change commits together with its audit event. Raising
    ``PersonaError`` leaves the state unchanged.
    """

    async def current(self) -> PersonaVersion | None: ...

    async def get(self, version_id: str) -> PersonaVersion:
        """Raises ``PersonaError`` NOT_FOUND."""
        ...

    async def create_draft(self, new: NewPersonaDraft) -> tuple[PersonaVersion, bool]:
        """A draft based on the active version. A repeated
        ``(author, idempotency_key)`` returns the original with ``False``
        (IDEMPOTENCY_CONFLICT when the text differs)."""
        ...

    async def update_draft(self, update: PersonaDraftUpdate) -> PersonaVersion:
        """Author only, draft only, CONFLICT unless ``expected_revision`` is
        current. Clears any earlier preview."""
        ...

    async def discard_draft(
        self, version_id: str, *, author_id: str, audit_id: str, at: datetime
    ) -> PersonaVersion: ...

    async def mark_previewed(
        self,
        version_id: str,
        *,
        digest: str,
        actor_id: str,
        audit_id: str,
        at: datetime,
    ) -> PersonaVersion:
        """Record that exactly ``digest`` was previewed (CONFLICT if the draft
        no longer has that text)."""
        ...

    async def publish(self, request: PublicationRequest) -> Publication:
        """One transaction: lock the active pointer, require the draft to be a
        draft, previewed with its current text and based on the active version,
        require ``expected_current`` to be active, move the pointer, freeze the
        version and append history and audit. NOT_PREVIEWED / CONFLICT /
        NOT_A_DRAFT otherwise."""
        ...

    async def rollback(self, request: PublicationRequest) -> Publication:
        """Make an earlier published version active again (same locking and
        ``expected_current`` check). NOT_PUBLISHED_BEFORE for anything else."""
        ...

    async def history(
        self, limit: int
    ) -> tuple[tuple[PersonaVersion, ...], tuple[Publication, ...], str | None]: ...

    async def pin_for_run(self, run_id: str, *, at: datetime) -> PersonaVersion | None:
        """The version pinned to ``run_id``; pins the active one (or records
        that none existed) the first time. Atomic with publication."""
        ...

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
        """Audit a refused publish/rollback (separate transaction)."""
        ...


class PreviewRenderer(Protocol):
    """Renders the sample findings under a persona section (or none)."""

    @property
    def applies_persona(self) -> bool: ...

    async def render(self, instructions: str | None, sample: SampleReport) -> str: ...
