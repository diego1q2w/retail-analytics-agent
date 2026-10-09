from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from retail_analytics.domain.conversation import Message
from retail_analytics.domain.executions import ToolExecution
from retail_analytics.domain.operations import SideEffect
from retail_analytics.domain.runs import ExecutionBackend, Run


class PersistenceError(Exception):
    """Base for typed persistence outcomes callers are expected to handle."""


class RecordNotFound(PersistenceError):
    def __init__(self, kind: str, key: str) -> None:
        self.kind = kind
        self.key = key
        super().__init__(f"{kind} {key!r} not found")


class IdempotencyConflict(PersistenceError):
    """The key was already used for a different request."""

    def __init__(self, kind: str, key: str) -> None:
        self.kind = kind
        self.key = key
        super().__init__(f"{kind} key {key!r} was already used for other content")


class ActiveRunExists(PersistenceError):
    """The session already has an active investigation."""

    def __init__(self, session_id: str, active_run_id: str) -> None:
        self.session_id = session_id
        self.active_run_id = active_run_id
        super().__init__(f"session {session_id!r} has active run {active_run_id!r}")


@dataclass(frozen=True, slots=True)
class RunRequest:
    """A user request that starts an investigation in a session."""

    run_id: str
    session_id: str
    requested_by: str
    submission_key: str
    message_id: str
    request_text: str
    # The backend that will execute (and alone may manage) the run.
    execution_backend: ExecutionBackend = ExecutionBackend.TEMPORAL


@dataclass(frozen=True, slots=True)
class RunStart:
    run: Run
    message: Message
    # False when the submission key matched an earlier request.
    created: bool


@dataclass(frozen=True, slots=True)
class OperationRequest:
    operation_id: str
    run_id: str
    capability: str
    capability_version: int
    side_effect: SideEffect
    deadline_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class OperationStart:
    execution: ToolExecution
    # False when the operation ID was already recorded (a retry or duplicate).
    created: bool
