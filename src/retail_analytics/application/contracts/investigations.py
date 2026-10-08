from __future__ import annotations

from dataclasses import dataclass

from retail_analytics.application.contracts.authorization import Principal
from retail_analytics.domain.runs import (
    Run,
    RunStatus,
)


@dataclass(frozen=True)
class RecoveryCandidate:
    run_id: str
    session_id: str
    principal: Principal
    request_text: str
    submission_key: str
    status: RunStatus


@dataclass(frozen=True, slots=True)
class AssistantOutput:
    """A released assistant message written together with a run-state change."""

    message_id: str
    content: str

    def __repr__(self) -> str:
        return f"AssistantOutput({self.message_id!r}, <{len(self.content)} chars>)"


@dataclass(frozen=True, slots=True)
class RunClosure:
    run: Run
    # False when pending input kept the run open (nothing changed).
    closed: bool
