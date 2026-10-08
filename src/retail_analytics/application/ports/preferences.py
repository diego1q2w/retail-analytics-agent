from __future__ import annotations

from typing import Protocol

from retail_analytics.application.contracts.preferences import (
    ObservationResult,
    ResolveResult,
    SaveResult,
)
from retail_analytics.domain.periods import OverrideScope
from retail_analytics.domain.preferences import (
    InferenceProposal,
    Preference,
    PreferenceSetting,
    PreferenceSource,
)


class PreferenceStore(Protocol):
    """Durable preferences, proposals and their append-only audit trail.

    Each method is one atomic change including its audit event. Audit events
    record who/what slot/when/which action and version, never the values.
    """

    async def save(
        self,
        executive_id: str,
        setting: PreferenceSetting,
        scope: OverrideScope,
        session_id: str | None,
        source: PreferenceSource,
    ) -> SaveResult:
        """Create or change; an identical value and source is a no-op."""
        ...

    async def list_preferences(
        self, executive_id: str, session_id: str | None
    ) -> tuple[Preference, ...]:
        """Saved defaults plus, when given, that session's preferences."""
        ...

    async def forget(
        self,
        executive_id: str,
        slot: str,
        scope: OverrideScope,
        session_id: str | None,
    ) -> Preference | None:
        """Delete one preference; returns what was removed, if anything."""
        ...

    async def forget_all(self, executive_id: str) -> tuple[Preference, ...]:
        """Delete every preference and every pending proposal of the executive."""
        ...

    async def observe(
        self, executive_id: str, session_id: str, setting: PreferenceSetting
    ) -> ObservationResult:
        """Count one observed repetition. Never persists a default."""
        ...

    async def open_proposals(
        self, executive_id: str, session_id: str | None
    ) -> tuple[InferenceProposal, ...]: ...

    async def resolve_proposal(
        self, executive_id: str, proposal_id: str, *, confirm: bool
    ) -> ResolveResult:
        """Confirm (saving the default atomically) or decline an open proposal.

        Raises ``AccessDenied`` when unknown/not owned and ``InvalidTransition``
        when it is not open (already resolved or expired).
        """
        ...


class FindingInvalidator(Protocol):
    """Marks findings computed under an old meaning as needing recalculation."""

    async def invalidate_dependent_findings(
        self, executive_id: str, session_id: str | None, slot: str
    ) -> None:
        """``session_id`` None means every session of the executive."""
        ...
