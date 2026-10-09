from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from retail_analytics.domain.persona import (
    PersonaFinding,
    PersonaVersion,
    Publication,
)


@dataclass(frozen=True, slots=True)
class NewPersonaDraft:
    version_id: str
    audit_id: str
    author_id: str
    content: str
    findings: tuple[PersonaFinding, ...]
    idempotency_key: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PersonaDraftUpdate:
    version_id: str
    audit_id: str
    author_id: str
    content: str
    findings: tuple[PersonaFinding, ...]
    expected_revision: int
    at: datetime


@dataclass(frozen=True, slots=True)
class PublicationRequest:
    """Publish a draft or roll back to an earlier published version.

    ``expected_current`` is the active version the caller looked at (None when
    nothing was published). The change is refused if it moved since.
    """

    version_id: str
    expected_current: str | None
    actor_id: str
    audit_id: str
    at: datetime


@dataclass(frozen=True, slots=True)
class PersonaPreview:
    """Current and proposed persona over the same sample findings."""

    draft_id: str
    draft_number: int
    current_version_id: str | None
    current_number: int | None
    findings: tuple[PersonaFinding, ...]
    current_instructions: str | None
    proposed_instructions: str
    sample_current: str
    sample_proposed: str
    # Tokens of the sample (figures, evidence ids, limitations) missing from
    # each rendering. Publishing requires none missing on the proposed side.
    missing_current: tuple[str, ...]
    missing_proposed: tuple[str, ...]
    # False when the renderer did not apply the persona (the offline
    # canonical renderer only lays the sample out).
    persona_applied: bool

    @property
    def preserved(self) -> bool:
        return not self.missing_proposed


@dataclass(frozen=True, slots=True)
class PersonaHistory:
    versions: tuple[PersonaVersion, ...]
    publications: tuple[Publication, ...]
    current_version_id: str | None
