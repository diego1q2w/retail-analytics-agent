"""Rules for what earlier conversation may re-enter model context.

Stored history is larger than model context and was produced under the
authority current at the time. Before a message re-enters context, these
rules decide whether it is still safe and still meaningful under the
caller's *current* authority:

- A message whose run used evidence that current authority no longer allows
  (authorization version or product set changed, empty scope, tampering) may
  carry restricted figures: assistant text is withheld, user text keeps its
  words but loses its figures and references.
- A message whose run used invalidated evidence (a changed definition or
  preference) is superseded: assistant text is withheld as no longer current.
- Messages are compared with evidence by time as well, because a message can
  repeat earlier figures: once evidence computed under the current authority
  exists, everything after it was produced under that same authority
  (authorization versions only increase), so it is clean; anything older than
  that point but newer than evidence that is now withheld is treated as
  possibly repeating it.
- A topic reset excludes everything before it from context (it deletes
  nothing).

Information already shown to the user cannot be unseen; these rules only
stop it from flowing back into new model work.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from retail_analytics.domain.conversation import Message, MessageRole
from retail_analytics.domain.evidence import Evidence, ReuseBlock

# Rough characters-per-token ratio used for budget accounting; the budget is
# a bound on prompt size, not a billing estimate.
CHARS_PER_TOKEN = 4


@dataclass(frozen=True, slots=True)
class ContextBudget:
    """Upper bounds for one assembled context.

    ``max_tokens`` bounds everything selected (estimated from characters);
    the current request is always included, truncated to
    ``max_request_chars`` if needed.
    """

    max_tokens: int = 8_000
    max_request_chars: int = 4_000
    max_history_messages: int = 12
    max_message_chars: int = 2_000
    max_evidence: int = 6
    max_rows_per_evidence: int = 20
    history_scan: int = 60
    evidence_scan: int = 200

    def __post_init__(self) -> None:
        if self.max_tokens < 500 or self.max_request_chars < 200:
            raise ValueError("context budget is too small")
        if (
            min(
                self.max_history_messages,
                self.max_message_chars,
                self.max_evidence,
                self.max_rows_per_evidence,
                self.history_scan,
                self.evidence_scan,
            )
            < 0
        ):
            raise ValueError("context limits must not be negative")

    @property
    def max_chars(self) -> int:
        return self.max_tokens * CHARS_PER_TOKEN


class HistoryTreatment(StrEnum):
    INCLUDE = "include"
    # Words kept, figures and references removed.
    STRIP_FIGURES = "strip_figures"
    WITHHELD_ACCESS_CHANGED = "withheld_access_changed"
    WITHHELD_SUPERSEDED = "withheld_superseded"
    BEFORE_RESET = "before_reset"


@dataclass(frozen=True, slots=True)
class EvidenceStanding:
    """One stored evidence record judged against the caller's current authority."""

    evidence: Evidence
    # None: usable. Otherwise the first authority rule it fails.
    block: ReuseBlock | None

    @property
    def usable(self) -> bool:
        return self.block is None

    @property
    def access_withdrawn(self) -> bool:
        """Withheld because of authority (not merely a changed meaning)."""
        return self.block is not None and self.block is not ReuseBlock.INVALIDATED

    @property
    def superseded(self) -> bool:
        return self.block is ReuseBlock.INVALIDATED


@dataclass(frozen=True, slots=True)
class HistoryRules:
    """Facts about the session needed to judge each message."""

    standings: Mapping[str, EvidenceStanding]
    # Evidence produced or reused per run.
    run_evidence: Mapping[str, frozenset[str]]
    reset_at: datetime | None = None

    @property
    def current_since(self) -> datetime | None:
        """Earliest computation under the current authority, if any."""
        times = [s.evidence.computed_at for s in self.standings.values() if s.usable]
        return min(times) if times else None

    @property
    def withdrawn_since(self) -> datetime | None:
        """Earliest computation of evidence now withheld for access reasons."""
        times = [
            s.evidence.computed_at
            for s in self.standings.values()
            if s.access_withdrawn
        ]
        return min(times) if times else None

    def treatment(self, message: Message) -> HistoryTreatment:
        if self.reset_at is not None and message.created_at < self.reset_at:
            return HistoryTreatment.BEFORE_RESET
        linked = [
            self.standings[e]
            for e in self.run_evidence.get(message.run_id or "", frozenset())
            if e in self.standings
        ]
        unknown = [
            e
            for e in self.run_evidence.get(message.run_id or "", frozenset())
            if e not in self.standings
        ]
        assistant = message.role is MessageRole.ASSISTANT
        if unknown or any(s.access_withdrawn for s in linked):
            return (
                HistoryTreatment.WITHHELD_ACCESS_CHANGED
                if assistant
                else HistoryTreatment.STRIP_FIGURES
            )
        if assistant and any(s.superseded for s in linked):
            return HistoryTreatment.WITHHELD_SUPERSEDED
        current_since = self.current_since
        if current_since is not None and message.created_at >= current_since:
            return HistoryTreatment.INCLUDE
        withdrawn_since = self.withdrawn_since
        if withdrawn_since is not None and message.created_at >= withdrawn_since:
            return (
                HistoryTreatment.WITHHELD_ACCESS_CHANGED
                if assistant
                else HistoryTreatment.STRIP_FIGURES
            )
        return HistoryTreatment.INCLUDE


def standings_by_id(
    standings: Iterable[EvidenceStanding],
) -> dict[str, EvidenceStanding]:
    return {s.evidence.evidence_id: s for s in standings}
