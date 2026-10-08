"""Ports to durable application state: sessions, runs, operations, run events.

Each port is narrow and role-specific; use cases depend on the ones they need
and bootstrap injects PostgreSQL implementations. Records are domain objects,
never ORM rows. Writes that a retry can repeat are idempotent on an
application-generated key (session/message/run ID, run submission key,
operation ID); repeating one with different content is a typed conflict, not
a silent overwrite.

Repositories check record linkage (a run belongs to its session and
requester) but not authorization: use cases authorize first.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from retail_analytics.application.progress import ProgressEvent, ProgressUpdate
from retail_analytics.domain.conversation import Message, MessageRole, Session
from retail_analytics.domain.executions import (
    ExecutionEvent,
    QueryJob,
    ToolExecution,
    ToolExecutionStatus,
)
from retail_analytics.domain.operations import SideEffect, ToolErrorCode
from retail_analytics.domain.runs import Run, RunStatus, WorkflowRef


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


class SessionRepository(Protocol):
    async def create_session(self, session_id: str, executive_id: str) -> Session:
        """Create, or return the same executive's existing session."""
        ...

    async def get_session(self, session_id: str) -> Session | None: ...

    async def append_message(
        self,
        *,
        message_id: str,
        session_id: str,
        role: MessageRole,
        content: str,
        run_id: str | None = None,
    ) -> Message:
        """Append once; repeating the same message ID returns the original."""
        ...

    async def recent_messages(self, session_id: str, limit: int) -> Sequence[Message]:
        """The latest ``limit`` messages, oldest first."""
        ...


@dataclass(frozen=True, slots=True)
class RunRequest:
    """A user request that starts an investigation in a session."""

    run_id: str
    session_id: str
    requested_by: str
    submission_key: str
    message_id: str
    request_text: str


@dataclass(frozen=True, slots=True)
class RunStart:
    run: Run
    message: Message
    # False when the submission key matched an earlier request.
    created: bool


class RunRepository(Protocol):
    async def start_run(self, request: RunRequest) -> RunStart:
        """Atomically record the triggering message and a running run.

        Raises ``ActiveRunExists`` if another run in the session is active and
        ``RecordNotFound`` if the session does not exist for the requester.
        """
        ...

    async def get_run(self, run_id: str) -> Run | None: ...

    async def active_run(self, session_id: str) -> Run | None: ...

    async def transition_run(self, run_id: str, to: RunStatus) -> Run:
        """Apply a lifecycle transition (no-op if already there)."""
        ...

    async def attach_workflow(self, run_id: str, workflow: WorkflowRef) -> Run:
        """Record the Temporal execution of the run, once."""
        ...


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


class ToolExecutionRepository(Protocol):
    async def begin(self, request: OperationRequest) -> OperationStart:
        """Record a PREPARED operation exactly once per operation ID."""
        ...

    async def get(self, operation_id: str) -> ToolExecution | None: ...

    async def transition(
        self,
        operation_id: str,
        to: ToolExecutionStatus,
        *,
        attempt: int,
        error_code: ToolErrorCode | None = None,
        detail: str | None = None,
    ) -> ToolExecution:
        """Update current status and append the transition in one transaction."""
        ...

    async def history(self, operation_id: str) -> Sequence[ExecutionEvent]: ...

    async def for_run(self, run_id: str) -> Sequence[ToolExecution]: ...


class QueryJobRepository(Protocol):
    async def register_job(self, job: QueryJob) -> QueryJob:
        """Record a job reference before it is submitted.

        Idempotent per (operation, submission): the same content returns the
        record, different content raises ``IdempotencyConflict``, as does a
        submission that does not directly follow the latest one or a job ID
        already used elsewhere.
        """
        ...

    async def get_job(self, operation_id: str) -> QueryJob | None:
        """The operation's latest submission, if any."""
        ...

    async def jobs(self, operation_id: str) -> Sequence[QueryJob]:
        """Every submission of the operation, in order."""
        ...


class RunEventStore(Protocol):
    """Ordered, append-only user-visible progress of a run, for SSE replay.

    Also satisfies ``ProgressSink`` (``publish``).
    """

    async def append(self, update: ProgressUpdate) -> ProgressEvent:
        """Persist and stamp the next event of the update's run."""
        ...

    async def publish(self, update: ProgressUpdate) -> None: ...

    async def replay(
        self, run_id: str, *, after_event_id: str | None = None, limit: int = 500
    ) -> Sequence[ProgressEvent]:
        """Events after ``after_event_id`` (all when ``None``), in order.

        An event ID that does not belong to the run raises ``RecordNotFound``.
        """
        ...
