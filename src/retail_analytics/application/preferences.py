"""Preference memory use cases: remember, inspect, change, forget, confirm.

Every operation first checks the caller's current permission and ownership
(``AccessResolver`` / ``OwnershipGuard``), then works only on that executive's
own preferences; another executive's session or proposal looks the same as a
missing one. Preferences select among reviewed options and are never an input
to product scope, so they cannot widen access.

Explicit requests persist at the scope the user chose. Repeated behaviour
(``observe``) adapts only the current session and produces a proposal that
persists solely through an explicit ``confirm``. Changing or forgetting a
setting that affects computed numbers invalidates dependent findings through
``FindingInvalidator``; formatting changes never do.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from retail_analytics.application.authorization import (
    AccessDenied,
    AccessResolver,
    OwnershipGuard,
)
from retail_analytics.application.contracts.authorization import Principal
from retail_analytics.application.contracts.preferences import (
    ObservationResult,
    ResolveResult,
    SaveResult,
)
from retail_analytics.application.ports.preferences import (
    FindingInvalidator,
    PreferenceStore,
)
from retail_analytics.domain.access import Permission
from retail_analytics.domain.metrics import MetricCatalog, UnknownMetricError
from retail_analytics.domain.periods import OverrideScope
from retail_analytics.domain.preferences import (
    PERSISTED_SCOPES,
    EffectivePreferences,
    InferenceProposal,
    InvalidPreference,
    Preference,
    PreferenceKind,
    PreferenceSetting,
    PreferenceSource,
    parse_slot,
)


class PreferenceAction(StrEnum):
    REMEMBERED = "remembered"
    CHANGED = "changed"
    UNCHANGED = "unchanged"
    FORGOTTEN = "forgotten"
    NOT_FOUND = "not_found"
    PROPOSED = "proposed"
    OBSERVED = "observed"
    DECLINED = "declined"
    CONFIRMED = "confirmed"


@dataclass(frozen=True, slots=True)
class PreferenceOutcome:
    action: PreferenceAction
    message: str
    slot: str | None = None
    scope: OverrideScope | None = None
    version: int | None = None
    invalidated_findings: bool = False
    proposal_id: str | None = None


@dataclass(frozen=True, slots=True)
class PreferenceView:
    preferences: tuple[Preference, ...]
    proposals: tuple[InferenceProposal, ...]


class PreferenceService:
    def __init__(
        self,
        store: PreferenceStore,
        resolver: AccessResolver,
        guard: OwnershipGuard,
        catalog: MetricCatalog,
        invalidator: FindingInvalidator | None = None,
    ) -> None:
        self._store = store
        self._resolver = resolver
        self._guard = guard
        self._catalog = catalog
        self._invalidator = invalidator

    async def _authorize(self, principal: Principal, session_id: str | None) -> str:
        access = await self._resolver.require_permission(
            principal, Permission.ANALYSIS_READ
        )
        if session_id is not None:
            await self._guard.session(access.executive_id, session_id)
        return access.executive_id

    def _validate(self, setting: PreferenceSetting) -> None:
        if setting.kind is PreferenceKind.METRIC_DEFINITION:
            metric_id, version = setting.metric_ref
            try:
                self._catalog.get(metric_id, version)
            except UnknownMetricError:
                raise InvalidPreference(
                    f"{metric_id} version {version} is not an approved metric"
                ) from None

    def _describe(self, setting: PreferenceSetting) -> str:
        if setting.kind is PreferenceKind.METRIC_DEFINITION:
            metric_id, version = setting.metric_ref
            meaning = self._catalog.get(metric_id, version)
            return (
                f"'{setting.term}' means {metric_id} v{version} ({meaning.description})"
            )
        return f"{self._label(setting)}: {setting.value}"

    @staticmethod
    def _label(setting: PreferenceSetting) -> str:
        return setting.kind.value.replace("_", " ")

    @staticmethod
    def _where(scope: OverrideScope) -> str:
        return (
            "for this session"
            if scope is OverrideScope.SESSION
            else "for all future sessions"
        )

    async def remember(
        self,
        principal: Principal,
        setting: PreferenceSetting,
        *,
        scope: OverrideScope = OverrideScope.USER_DEFAULT,
        session_id: str | None = None,
    ) -> PreferenceOutcome:
        """Explicit remember/change request; acknowledges the saved meaning."""
        if scope not in PERSISTED_SCOPES:
            raise InvalidPreference(
                "temporary instructions are applied per report, not saved"
            )
        if scope is OverrideScope.SESSION and session_id is None:
            raise InvalidPreference("a session preference needs a session")
        executive_id = await self._authorize(principal, session_id)
        self._validate(setting)
        result = await self._store.save(
            executive_id,
            setting,
            scope,
            session_id if scope is OverrideScope.SESSION else None,
            PreferenceSource.EXPLICIT,
        )
        pref = result.preference
        where = self._where(scope)
        if not result.changed:
            return PreferenceOutcome(
                PreferenceAction.UNCHANGED,
                f"Already set {where}: {self._describe(setting)}.",
                setting.slot,
                scope,
                pref.version,
            )
        invalidated = result.meaning_changed and await self._invalidate(
            executive_id, pref
        )
        verb = (
            PreferenceAction.REMEMBERED
            if result.previous is None
            else PreferenceAction.CHANGED
        )
        note = (
            " Earlier findings that depend on it will be recalculated."
            if invalidated
            else ""
        )
        return PreferenceOutcome(
            verb,
            f"Saved {where}: {self._describe(setting)}.{note}",
            setting.slot,
            scope,
            pref.version,
            invalidated,
        )

    async def _invalidate(self, executive_id: str, pref: Preference) -> bool:
        if not pref.setting.kind.changes_analysis:
            return False
        if self._invalidator is not None:
            await self._invalidator.invalidate_dependent_findings(
                executive_id,
                pref.session_id if pref.scope is OverrideScope.SESSION else None,
                pref.setting.slot,
            )
        return True

    async def inspect(
        self, principal: Principal, *, session_id: str | None = None
    ) -> PreferenceView:
        executive_id = await self._authorize(principal, session_id)
        return PreferenceView(
            await self._store.list_preferences(executive_id, session_id),
            await self._store.open_proposals(executive_id, session_id),
        )

    async def effective(
        self,
        principal: Principal,
        *,
        session_id: str | None = None,
        temporary: Sequence[PreferenceSetting] = (),
    ) -> EffectivePreferences:
        """What applies now; ``temporary`` are report-scoped and not stored."""
        executive_id = await self._authorize(principal, session_id)
        stored = await self._store.list_preferences(executive_id, session_id)
        for setting in temporary:
            self._validate(setting)
        return EffectivePreferences.build(stored, tuple(temporary))

    async def forget(
        self,
        principal: Principal,
        slot: str,
        *,
        scope: OverrideScope = OverrideScope.USER_DEFAULT,
        session_id: str | None = None,
    ) -> PreferenceOutcome:
        parse_slot(slot)
        if scope not in PERSISTED_SCOPES:
            raise InvalidPreference("only saved preferences can be forgotten")
        if scope is OverrideScope.SESSION and session_id is None:
            raise InvalidPreference("a session preference needs a session")
        executive_id = await self._authorize(principal, session_id)
        removed = await self._store.forget(
            executive_id,
            slot,
            scope,
            session_id if scope is OverrideScope.SESSION else None,
        )
        if removed is None:
            return PreferenceOutcome(
                PreferenceAction.NOT_FOUND, "Nothing was saved for that.", slot, scope
            )
        invalidated = await self._invalidate(executive_id, removed)
        return PreferenceOutcome(
            PreferenceAction.FORGOTTEN,
            f"Forgot {self._where(scope)}: {self._label(removed.setting)}.",
            slot,
            scope,
            invalidated_findings=invalidated,
        )

    async def forget_everything(self, principal: Principal) -> int:
        executive_id = await self._authorize(principal, None)
        removed = await self._store.forget_all(executive_id)
        for pref in removed:
            await self._invalidate(executive_id, pref)
        return len(removed)

    async def observe(
        self, principal: Principal, session_id: str, setting: PreferenceSetting
    ) -> PreferenceOutcome:
        """Record that the user repeated a behaviour (an explicit signal only).

        After enough repetitions the session adapts and a proposal to remember
        it is raised; nothing is saved across sessions until ``confirm``.
        """
        executive_id = await self._authorize(principal, session_id)
        self._validate(setting)
        result = await self._store.observe(executive_id, session_id, setting)
        if not result.newly_proposed:
            return PreferenceOutcome(
                PreferenceAction.OBSERVED,
                "Noted for this session only.",
                setting.slot,
                proposal_id=result.proposal.proposal_id,
            )
        await self._store.save(
            executive_id,
            setting,
            OverrideScope.SESSION,
            session_id,
            PreferenceSource.INFERRED_SESSION,
        )
        return PreferenceOutcome(
            PreferenceAction.PROPOSED,
            f"I have been using {self._describe(setting)} in this session. "
            "Should I remember it for future sessions?",
            setting.slot,
            OverrideScope.SESSION,
            proposal_id=result.proposal.proposal_id,
        )

    async def confirm(
        self, principal: Principal, proposal_id: str
    ) -> PreferenceOutcome:
        return await self._resolve(principal, proposal_id, confirm=True)

    async def decline(
        self, principal: Principal, proposal_id: str
    ) -> PreferenceOutcome:
        return await self._resolve(principal, proposal_id, confirm=False)

    async def _resolve(
        self, principal: Principal, proposal_id: str, *, confirm: bool
    ) -> PreferenceOutcome:
        executive_id = await self._authorize(principal, None)
        result = await self._store.resolve_proposal(
            executive_id, proposal_id, confirm=confirm
        )
        slot = result.proposal.setting.slot
        if not confirm or result.preference is None:
            return PreferenceOutcome(
                PreferenceAction.DECLINED,
                "Okay, I will not remember it.",
                slot,
                proposal_id=proposal_id,
            )
        invalidated = await self._invalidate(executive_id, result.preference)
        return PreferenceOutcome(
            PreferenceAction.CONFIRMED,
            "Saved for all future sessions: "
            f"{self._describe(result.preference.setting)}.",
            slot,
            OverrideScope.USER_DEFAULT,
            result.preference.version,
            invalidated,
            proposal_id,
        )


__all__ = [
    "AccessDenied",
    "FindingInvalidator",
    "ObservationResult",
    "PreferenceAction",
    "PreferenceOutcome",
    "PreferenceService",
    "PreferenceStore",
    "PreferenceView",
    "ResolveResult",
    "SaveResult",
]
