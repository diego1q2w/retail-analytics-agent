from __future__ import annotations

from dataclasses import dataclass

from retail_analytics.application.contracts.progress import ProgressEvent
from retail_analytics.domain.conversation import Session
from retail_analytics.domain.runs import Run, RunStatus


@dataclass(frozen=True, slots=True)
class SessionOverview:
    session: Session
    # Newest first.
    runs: tuple[Run, ...]


@dataclass(frozen=True, slots=True)
class ReleasedText:
    """Generated text as it may be shown now. ``withheld`` means the output
    gate refused it and ``text`` is the safe explanation instead."""

    text: str
    withheld: bool = False

    def __repr__(self) -> str:
        return f"ReleasedText(<{len(self.text)} chars>, withheld={self.withheld})"


@dataclass(frozen=True, slots=True)
class OpenQuestion:
    question_id: str
    text: ReleasedText


@dataclass(frozen=True, slots=True)
class RunView:
    run: Run
    question: OpenQuestion | None
    # Set once the run is terminal and released an answer or findings.
    answer: ReleasedText | None


@dataclass(frozen=True, slots=True)
class EventBatch:
    """Gap-free events after a cursor, each released through the output gate
    under authority resolved after the events were read."""

    events: tuple[ProgressEvent, ...]
    status: RunStatus
    # True when the batch is shorter than the limit (the client caught up).
    caught_up: bool
