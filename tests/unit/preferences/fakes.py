"""In-memory PreferenceStore obeying the same contract as the PostgreSQL one."""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from retail_analytics.application.authorization import AccessDenied
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
    ProposalStatus,
)


@dataclass
class Clock:
    now: datetime = datetime(2026, 10, 8, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@dataclass
class Event:
    executive_id: str
    slot: str
    action: str
    version: int | None


@dataclass
class FakePreferenceStore:
    clock: Callable[[], datetime] = field(default_factory=Clock)
    prefs: dict[str, Preference] = field(default_factory=dict)
    proposals: dict[str, InferenceProposal] = field(default_factory=dict)
    events: list[Event] = field(default_factory=list)
    _ids: itertools.count[int] = field(default_factory=itertools.count)

    def _key(
        self, executive_id: str, slot: str, scope: OverrideScope, session_id: str | None
    ) -> str | None:
        for pid, p in self.prefs.items():
            if (p.executive_id, p.setting.slot, p.scope, p.session_id) == (
                executive_id,
                slot,
                scope,
                session_id,
            ):
                return pid
        return None

    def _save(
        self,
        executive_id: str,
        setting: PreferenceSetting,
        scope: OverrideScope,
        session_id: str | None,
        source: PreferenceSource,
        event: str | None = None,
    ) -> SaveResult:
        now = self.clock()
        pid = self._key(executive_id, setting.slot, scope, session_id)
        previous = None if pid is None else self.prefs[pid]
        if previous is None:
            pid = f"p{next(self._ids)}"
            current = Preference(
                pid, executive_id, setting, scope, session_id, source, 1, now, now
            )
            action = "remembered"
        elif (previous.setting, previous.source) == (setting, source):
            return SaveResult(previous, previous)
        else:
            current = previous.changed(setting, source, now)
            action = "changed"
        assert pid is not None
        self.prefs[pid] = current
        if source is PreferenceSource.INFERRED_SESSION:
            action = "adapted"
        self.events.append(
            Event(executive_id, setting.slot, event or action, current.version)
        )
        return SaveResult(current, previous)

    async def save(
        self,
        executive_id: str,
        setting: PreferenceSetting,
        scope: OverrideScope,
        session_id: str | None,
        source: PreferenceSource,
    ) -> SaveResult:
        return self._save(executive_id, setting, scope, session_id, source)

    async def list_preferences(
        self, executive_id: str, session_id: str | None
    ) -> tuple[Preference, ...]:
        return tuple(
            p
            for p in self.prefs.values()
            if p.executive_id == executive_id
            and (p.scope is OverrideScope.USER_DEFAULT or p.session_id == session_id)
        )

    async def forget(
        self,
        executive_id: str,
        slot: str,
        scope: OverrideScope,
        session_id: str | None,
    ) -> Preference | None:
        pid = self._key(executive_id, slot, scope, session_id)
        if pid is None:
            return None
        removed = self.prefs.pop(pid)
        self.events.append(Event(executive_id, slot, "forgotten", removed.version))
        return removed

    async def forget_all(self, executive_id: str) -> tuple[Preference, ...]:
        mine = [pid for pid, p in self.prefs.items() if p.executive_id == executive_id]
        removed = tuple(self.prefs.pop(pid) for pid in mine)
        for pid in [
            k for k, v in self.proposals.items() if v.executive_id == executive_id
        ]:
            del self.proposals[pid]
        for p in removed:
            self.events.append(
                Event(executive_id, p.setting.slot, "forgotten", p.version)
            )
        return removed

    async def observe(
        self, executive_id: str, session_id: str, setting: PreferenceSetting
    ) -> ObservationResult:
        now = self.clock()
        found = next(
            (
                p
                for p in self.proposals.values()
                if (p.executive_id, p.session_id, p.setting)
                == (executive_id, session_id, setting)
            ),
            None,
        )
        if found is None:
            declined = any(
                p.executive_id == executive_id
                and p.setting == setting
                and p.status is ProposalStatus.DECLINED
                for p in self.proposals.values()
            )
            found = InferenceProposal(
                f"q{next(self._ids)}",
                executive_id,
                session_id,
                setting,
                0,
                ProposalStatus.DECLINED if declined else ProposalStatus.OBSERVING,
                now,
                now,
            )
        after = found.observe(now)
        self.proposals[after.proposal_id] = after
        newly = (
            found.status is ProposalStatus.OBSERVING
            and after.status is ProposalStatus.PROPOSED
        )
        if newly:
            self.events.append(Event(executive_id, setting.slot, "proposed", None))
        return ObservationResult(after, newly)

    async def open_proposals(
        self, executive_id: str, session_id: str | None
    ) -> tuple[InferenceProposal, ...]:
        now = self.clock()
        return tuple(
            p
            for p in self.proposals.values()
            if p.executive_id == executive_id
            and p.is_open(now)
            and (session_id is None or p.session_id == session_id)
        )

    async def resolve_proposal(
        self, executive_id: str, proposal_id: str, *, confirm: bool
    ) -> ResolveResult:
        proposal = self.proposals.get(proposal_id)
        if proposal is None or proposal.executive_id != executive_id:
            raise AccessDenied("proposal", proposal_id)
        now = self.clock()
        resolved = proposal.confirm(now) if confirm else proposal.decline(now)
        self.proposals[proposal_id] = resolved
        if not confirm:
            self.events.append(
                Event(executive_id, proposal.setting.slot, "declined", None)
            )
            return ResolveResult(resolved, None)
        saved = self._save(
            executive_id,
            proposal.setting,
            OverrideScope.USER_DEFAULT,
            None,
            PreferenceSource.INFERRED_CONFIRMED,
            "confirmed",
        )
        return ResolveResult(resolved, saved.preference)


@dataclass
class RecordingInvalidator:
    calls: list[tuple[str, str | None, str]] = field(default_factory=list)

    async def invalidate_dependent_findings(
        self, executive_id: str, session_id: str | None, slot: str
    ) -> None:
        self.calls.append((executive_id, session_id, slot))
