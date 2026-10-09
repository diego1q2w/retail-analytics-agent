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
class CitedSource:
    """A recognized evidence citation in a released answer, for display.

    ``number`` is its first-use position among the answer's recognized
    citations; ``label`` is how it is shown (``1``, or ``S1`` when the answer
    has numbered references of its own). ``description`` is
    application-authored from trusted evidence metadata and has passed the
    output gate. ``current`` is False for a superseded result (a definition
    or preference changed after it was computed).
    """

    number: int
    label: str
    evidence_id: str
    kind: str
    description: str
    current: bool = True


@dataclass(frozen=True, slots=True)
class ReleasedText:
    """Generated text as it may be shown now. ``withheld`` means the output
    gate refused it and ``text`` is the safe explanation instead.
    ``citations`` (never set when withheld) maps the recognized evidence IDs
    in ``text`` to numbered sources; the text itself keeps the IDs."""

    text: str
    withheld: bool = False
    citations: tuple[CitedSource, ...] = ()

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
