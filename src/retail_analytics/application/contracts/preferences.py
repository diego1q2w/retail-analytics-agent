from __future__ import annotations

from dataclasses import dataclass

from retail_analytics.domain.preferences import (
    InferenceProposal,
    Preference,
)


@dataclass(frozen=True, slots=True)
class SaveResult:
    preference: Preference
    previous: Preference | None

    @property
    def changed(self) -> bool:
        """Anything about the stored preference changed (value or source)."""
        return self.previous is None or self.previous.version != self.preference.version

    @property
    def meaning_changed(self) -> bool:
        return self.previous is None or (
            self.previous.setting.value != self.preference.setting.value
        )


@dataclass(frozen=True, slots=True)
class ObservationResult:
    proposal: InferenceProposal
    newly_proposed: bool


@dataclass(frozen=True, slots=True)
class ResolveResult:
    proposal: InferenceProposal
    preference: Preference | None
