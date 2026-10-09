"""Inputs to a running investigation and the identifiers that keep them stable.

An investigation (run) receives user input after it starts: steering messages
that refine the active analysis, answers to the clarification it asked, and
explicitly queued separate requests that wait for the session's active run to
finish. Every input is recorded before the runtime is told about it, so a lost
notification, a worker restart or a reconnecting client never loses one; the
runtime reads the recorded log at safe boundaries instead of trusting signal
payloads.

Identifiers here are derived from stable keys (client submission keys, model
tool-call IDs) so that retries and resumption address the same records.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from retail_analytics.domain.errors import InvalidTransition

MAX_INPUT_CHARS = 8_000
MAX_QUESTION_CHARS = 1_000


class InputKind(StrEnum):
    # The request that started the run (recorded with it, applied at once).
    REQUEST = "request"
    # A later message that refines the active analysis (the default for new input).
    STEERING = "steering"
    # The reply to the run's open clarification question.
    ANSWER = "answer"
    # A separate request the user explicitly queued behind the active run.
    QUEUED = "queued"


class InputStatus(StrEnum):
    # Recorded, not yet seen by the investigation.
    PENDING = "pending"
    # Included in the investigation's context at a safe boundary.
    APPLIED = "applied"
    # A queued request that became its own run.
    PROMOTED = "promoted"
    # Not used (e.g. the run ended first); kept for the record. Steering or an
    # answer gets here only together with its run's end and notice.
    DISCARDED = "discarded"


_INPUT_TRANSITIONS = {
    InputStatus.PENDING: frozenset(
        {InputStatus.APPLIED, InputStatus.PROMOTED, InputStatus.DISCARDED}
    ),
}


@dataclass(frozen=True, slots=True)
class RunInput:
    """One recorded input. ``run_id`` is None for a queued request until it is
    promoted (then ``promoted_run_id`` names the run it became)."""

    input_id: str
    session_id: str
    kind: InputKind
    content: str
    status: InputStatus
    created_at: datetime
    run_id: str | None = None
    message_id: str | None = None
    question_id: str | None = None
    applied_at: datetime | None = None
    promoted_run_id: str | None = None

    def __post_init__(self) -> None:
        if not self.content.strip():
            raise ValueError("input content must not be empty")
        if len(self.content) > MAX_INPUT_CHARS:
            raise ValueError("input content is too long")
        if (self.kind is InputKind.QUEUED) != (self.run_id is None):
            raise ValueError("only queued requests have no run")
        if (self.kind is InputKind.ANSWER) != (self.question_id is not None):
            raise ValueError("answers, and only answers, name a question")

    def transition(
        self, to: InputStatus, *, at: datetime, promoted_run_id: str | None = None
    ) -> RunInput | None:
        """The input after moving to ``to``; ``None`` if it is already there."""
        if to is self.status:
            return None
        if to not in _INPUT_TRANSITIONS.get(self.status, frozenset()):
            raise InvalidTransition("run input", self.status, to)
        if (to is InputStatus.PROMOTED) != (promoted_run_id is not None):
            raise ValueError("a promoted input names its run")
        if to is InputStatus.PROMOTED and self.kind is not InputKind.QUEUED:
            raise InvalidTransition("run input", self.kind, to)
        return replace(
            self,
            status=to,
            applied_at=at if to is InputStatus.APPLIED else self.applied_at,
            promoted_run_id=promoted_run_id,
        )


class QuestionStatus(StrEnum):
    OPEN = "open"
    ANSWERED = "answered"
    # The run ended (cancelled, expired) before an answer arrived.
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class ClarificationQuestion:
    """A question the investigation is waiting on. Silence never answers it."""

    question_id: str
    run_id: str
    message_id: str
    status: QuestionStatus
    asked_at: datetime
    closed_at: datetime | None = None

    def close(
        self, to: QuestionStatus, *, at: datetime
    ) -> ClarificationQuestion | None:
        if to is self.status:
            return None
        if self.status is not QuestionStatus.OPEN or to is QuestionStatus.OPEN:
            raise InvalidTransition("clarification question", self.status, to)
        return replace(self, status=to, closed_at=at)


# What becomes of steering or an answer the run accepted (recorded first, so
# it is never silently lost; see ``unapplied_notice``):
# - applied: a later model step of the run includes it (``APPLIED``); an
#   answer the model drafted before it arrived is superseded, not released;
# - not applied: the run ends without another model step (budget or model
#   stop, decline, cancellation, interruption). Then, in the same transaction
#   that ends the run, it is marked ``DISCARDED`` and the run's closing
#   message says so. A finished answer never stands in for it.
_EXCERPT_CHARS = 80


def unapplied_notice(inputs: Sequence[RunInput]) -> str:
    """The explicit notice for accepted input the run ended without applying."""
    excerpts = "; ".join(f'"{_excerpt(item.content)}"' for item in inputs)
    if len(inputs) == 1:
        return (
            f"Note: your message {excerpts} arrived after this investigation's "
            "last step and was not applied to this answer. Send it again as a "
            "new request if you still need it."
        )
    return (
        f"Note: your {len(inputs)} messages {excerpts} arrived after this "
        "investigation's last step and were not applied to this answer. Send "
        "them again as a new request if you still need them."
    )


def unapplied_summary(count: int) -> str:
    """The progress-event summary of the same outcome (no user text)."""
    if count == 1:
        return (
            "Your last message was not applied: the investigation ended before "
            "its next step. Send it again if you still need it."
        )
    return (
        f"{count} of your messages were not applied: the investigation ended "
        "before its next step. Send them again if you still need them."
    )


APPLIED_SUMMARY = "Your message was applied; the investigation continues with it."


def _excerpt(text: str) -> str:
    flat = " ".join(text.split())
    if len(flat) <= _EXCERPT_CHARS:
        return flat
    return flat[: _EXCERPT_CHARS - 1] + "…"


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:32]


def run_id_for(session_id: str, submission_key: str) -> str:
    """The run a client submission creates; resubmitting finds the same run."""
    return "run_" + _digest("run", session_id, submission_key)


def input_id_for(scope: str, submission_key: str) -> str:
    """An input of a run (or a queued request of a session), per submission key."""
    return "inp_" + _digest("input", scope, submission_key)


def message_id_for(input_id: str) -> str:
    return "msg_" + _digest("message", input_id)


def operation_id_for(run_id: str, tool_call_id: str) -> str:
    """The durable operation of one model tool call.

    A replayed or resumed workflow sees the same recorded tool call, so its
    retried activity addresses the same operation (the idempotency key) and
    reconciles instead of repeating the effect.
    """
    return "op_" + _digest("operation", run_id, tool_call_id)


def answer_message_id(run_id: str, sequence: int) -> str:
    """The assistant message holding the run's ``sequence``-th released output."""
    return "msg_" + _digest("assistant", run_id, str(sequence))


def unapplied_message_id(run_id: str) -> str:
    """The notice message of a run that ended without output of its own."""
    return message_id_for(input_id_for(run_id, "unapplied"))


def question_id_for(run_id: str, sequence: int) -> str:
    return "qst_" + _digest("question", run_id, str(sequence))
