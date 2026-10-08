"""Cross-session preference memory: what an executive asked us to remember.

A preference is a small, typed setting (a metric meaning, display currency,
time zone, table format or level of detail) with its scope, source and version.
Values use closed grammars, so free text, product IDs, names or result rows can
never be stored here, and a preference selects among reviewed options without
touching authorization, privacy or source facts.

Scopes (narrowest first): ``REPORT`` is temporary and never stored; ``SESSION``
lasts for one session; ``USER_DEFAULT`` survives across sessions. Explicit
requests may be saved at any persisted scope. Behaviour the user merely repeats
may adapt the current session, but reaches ``USER_DEFAULT`` only through a
proposal the user explicitly confirms; silence and elapsed time never confirm.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum

from retail_analytics.domain.errors import InvalidTransition
from retail_analytics.domain.metric_preferences import DefinitionPreference
from retail_analytics.domain.metrics import MetricCatalog, UnknownMetricError
from retail_analytics.domain.periods import OverrideScope

PERSISTED_SCOPES = frozenset({OverrideScope.SESSION, OverrideScope.USER_DEFAULT})
# Same behaviour this many times in one session adapts that session.
ADAPT_THRESHOLD = 3
PROPOSAL_TTL = timedelta(days=7)

_TERM = re.compile(r"[a-z][a-z0-9_]{0,39}")
_METRIC_VALUE = re.compile(r"([a-z][a-z0-9_]{0,59})@([1-9][0-9]{0,3})")
_CURRENCY = re.compile(r"[A-Z]{3}")
_TABLE_FORMATS = frozenset({"table", "list", "prose"})
_DETAIL_LEVELS = frozenset({"brief", "standard", "detailed"})


class InvalidPreference(ValueError):
    """The requested preference is not a supported, well-formed setting."""


class PreferenceKind(StrEnum):
    METRIC_DEFINITION = "metric_definition"
    DISPLAY_CURRENCY = "display_currency"
    TIME_ZONE = "time_zone"
    TABLE_FORMAT = "table_format"
    DETAIL_LEVEL = "detail_level"

    @property
    def changes_analysis(self) -> bool:
        """Whether changing it can change computed numbers or periods.

        Display currency is presentation: source amounts stay in their source
        currency and a conversion is recorded as its own evidence.
        """
        return self in (PreferenceKind.METRIC_DEFINITION, PreferenceKind.TIME_ZONE)


class PreferenceSource(StrEnum):
    EXPLICIT = "explicit"
    INFERRED_CONFIRMED = "inferred_confirmed"
    INFERRED_SESSION = "inferred_session"


@dataclass(frozen=True, slots=True)
class PreferenceSetting:
    """One validated setting. ``term`` names the meaning for metric definitions."""

    kind: PreferenceKind
    value: str
    term: str | None = None

    def __post_init__(self) -> None:
        if self.kind is PreferenceKind.METRIC_DEFINITION:
            if self.term is None or not _TERM.fullmatch(self.term):
                raise InvalidPreference("metric preference needs a simple term")
            if not _METRIC_VALUE.fullmatch(self.value):
                raise InvalidPreference("metric value must be '<metric_id>@<version>'")
            return
        if self.term is not None:
            raise InvalidPreference(f"{self.kind.value} takes no term")
        if self.kind is PreferenceKind.DISPLAY_CURRENCY:
            ok = bool(_CURRENCY.fullmatch(self.value))
        elif self.kind is PreferenceKind.TIME_ZONE:
            ok = _known_zone(self.value)
        elif self.kind is PreferenceKind.TABLE_FORMAT:
            ok = self.value in _TABLE_FORMATS
        else:
            ok = self.value in _DETAIL_LEVELS
        if not ok:
            raise InvalidPreference(f"unsupported {self.kind.value} value")

    @classmethod
    def metric(cls, term: str, metric_id: str, version: int) -> PreferenceSetting:
        return cls(PreferenceKind.METRIC_DEFINITION, f"{metric_id}@{version}", term)

    @property
    def slot(self) -> str:
        """Identity of what is being set; one value per slot and scope."""
        if self.term is None:
            return self.kind.value
        return f"{self.kind.value}:{self.term}"

    @property
    def metric_ref(self) -> tuple[str, int]:
        match = _METRIC_VALUE.fullmatch(self.value)
        if self.kind is not PreferenceKind.METRIC_DEFINITION or match is None:
            raise InvalidPreference("not a metric preference")
        return match.group(1), int(match.group(2))


def _known_zone(name: str) -> bool:
    # Imported here: the zoneinfo module reads PYTHONTZPATH when first imported,
    # and inner layers must not read the environment at import time.
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    if not re.fullmatch(r"[A-Za-z0-9_+\-/]{1,40}", name):
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True


def parse_slot(slot: str) -> tuple[PreferenceKind, str | None]:
    """Inverse of ``PreferenceSetting.slot`` (for forget/inspect requests)."""
    kind_text, _, term = slot.partition(":")
    try:
        kind = PreferenceKind(kind_text)
    except ValueError:
        raise InvalidPreference(f"unknown preference {kind_text!r}") from None
    if (kind is PreferenceKind.METRIC_DEFINITION) != bool(term):
        raise InvalidPreference("metric preferences need a term; others take none")
    return kind, term or None


@dataclass(frozen=True, slots=True)
class Preference:
    """A stored preference owned by one executive."""

    preference_id: str
    executive_id: str
    setting: PreferenceSetting
    scope: OverrideScope
    session_id: str | None
    source: PreferenceSource
    version: int
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not self.executive_id or self.version < 1:
            raise InvalidPreference("preference needs an owner and version >= 1")
        if self.scope not in PERSISTED_SCOPES:
            raise InvalidPreference("temporary report preferences are never stored")
        if (self.scope is OverrideScope.SESSION) != (self.session_id is not None):
            raise InvalidPreference("session preferences (only) carry a session")
        if self.scope is OverrideScope.USER_DEFAULT and self.source not in (
            PreferenceSource.EXPLICIT,
            PreferenceSource.INFERRED_CONFIRMED,
        ):
            raise InvalidPreference(
                "a default needs an explicit request or confirmation"
            )
        if (
            self.source is PreferenceSource.INFERRED_SESSION
            and self.scope is not OverrideScope.SESSION
        ):
            raise InvalidPreference("an unconfirmed inference stays session-only")

    def changed(
        self, setting: PreferenceSetting, source: PreferenceSource, now: datetime
    ) -> Preference:
        if setting.slot != self.setting.slot:
            raise InvalidPreference("a change keeps the same slot")
        return replace(
            self,
            setting=setting,
            source=source,
            version=self.version + 1,
            updated_at=now,
        )

    @property
    def provenance(self) -> str:
        return f"{self.source.value} v{self.version}"


@dataclass(frozen=True, slots=True)
class EffectiveEntry:
    setting: PreferenceSetting
    scope: OverrideScope
    provenance: str


@dataclass(frozen=True, slots=True)
class EffectivePreferences:
    """What applies right now: report > session > saved default."""

    entries: tuple[EffectiveEntry, ...]

    @classmethod
    def build(
        cls,
        stored: tuple[Preference, ...],
        temporary: tuple[PreferenceSetting, ...] = (),
    ) -> EffectivePreferences:
        """Combine stored preferences with temporary (report-scoped) settings."""
        candidates = [
            EffectiveEntry(p.setting, p.scope, p.provenance) for p in stored
        ] + [
            EffectiveEntry(s, OverrideScope.REPORT, "temporary instruction")
            for s in temporary
        ]
        best: dict[str, EffectiveEntry] = {}
        order = {
            OverrideScope.REPORT: 0,
            OverrideScope.SESSION: 1,
            OverrideScope.USER_DEFAULT: 2,
        }
        for entry in candidates:
            current = best.get(entry.setting.slot)
            if current is None or order[entry.scope] < order[current.scope]:
                best[entry.setting.slot] = entry
        return cls(tuple(sorted(best.values(), key=lambda e: e.setting.slot)))

    def get(
        self, kind: PreferenceKind, term: str | None = None
    ) -> EffectiveEntry | None:
        for entry in self.entries:
            if entry.setting.kind is kind and entry.setting.term == term:
                return entry
        return None

    def value(self, kind: PreferenceKind) -> str | None:
        entry = self.get(kind)
        return None if entry is None else entry.setting.value

    def definition_preferences(
        self, catalog: MetricCatalog
    ) -> tuple[DefinitionPreference, ...]:
        """Metric meanings for ``resolve_term``; skips ones the catalog dropped."""
        found: list[DefinitionPreference] = []
        for entry in self.entries:
            if entry.setting.kind is not PreferenceKind.METRIC_DEFINITION:
                continue
            metric_id, version = entry.setting.metric_ref
            try:
                catalog.get(metric_id, version)
            except UnknownMetricError:
                continue
            term = entry.setting.term
            if term is None:
                continue
            found.append(
                DefinitionPreference(
                    term=term,
                    metric_id=metric_id,
                    metric_version=version,
                    scope=entry.scope,
                    provenance=entry.provenance,
                )
            )
        return tuple(found)

    @property
    def analytical_fingerprint(self) -> str:
        """Changes exactly when computed results could change.

        Evidence records the fingerprint it was computed under; a different one
        means it needs recomputation. Formatting and display settings are left
        out, so changing them reuses evidence and never forces new SQL.
        """
        parts = sorted(
            f"{e.setting.slot}={e.setting.value}"
            for e in self.entries
            if e.setting.kind.changes_analysis
        )
        return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]

    def invalidates(self, recorded_fingerprint: str) -> bool:
        return recorded_fingerprint != self.analytical_fingerprint


class ProposalStatus(StrEnum):
    OBSERVING = "observing"
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    DECLINED = "declined"


@dataclass(frozen=True, slots=True)
class InferenceProposal:
    """Repeated behaviour in one session, and whether we have asked to keep it.

    Only an explicit ``confirm`` before expiry makes it a default.
    """

    proposal_id: str
    executive_id: str
    session_id: str
    setting: PreferenceSetting
    observations: int
    status: ProposalStatus
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None = None

    def observe(self, now: datetime) -> InferenceProposal:
        if self.status is not ProposalStatus.OBSERVING:
            return self
        count = self.observations + 1
        if count >= ADAPT_THRESHOLD:
            return replace(
                self,
                observations=count,
                status=ProposalStatus.PROPOSED,
                updated_at=now,
                expires_at=now + PROPOSAL_TTL,
            )
        return replace(self, observations=count, updated_at=now)

    @property
    def adapts_session(self) -> bool:
        return self.observations >= ADAPT_THRESHOLD

    def is_open(self, now: datetime) -> bool:
        return (
            self.status is ProposalStatus.PROPOSED
            and self.expires_at is not None
            and now < self.expires_at
        )

    def confirm(self, now: datetime) -> InferenceProposal:
        return self._resolve(now, ProposalStatus.CONFIRMED)

    def decline(self, now: datetime) -> InferenceProposal:
        return self._resolve(now, ProposalStatus.DECLINED)

    def _resolve(self, now: datetime, target: ProposalStatus) -> InferenceProposal:
        if not self.is_open(now):
            raise InvalidTransition(
                "preference proposal", self.status.value, target.value
            )
        return replace(self, status=target, updated_at=now)
