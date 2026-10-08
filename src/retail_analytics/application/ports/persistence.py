from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from retail_analytics.application.contracts.persistence import (
    OperationRequest,
    OperationStart,
    RunRequest,
    RunStart,
)
from retail_analytics.application.contracts.progress import (
    ProgressEvent,
    ProgressUpdate,
)
from retail_analytics.domain.conversation import (
    Message,
    MessageRole,
    Session,
)
from retail_analytics.domain.executions import (
    ExecutionEvent,
    QueryJob,
    ToolExecution,
    ToolExecutionStatus,
)
from retail_analytics.domain.operations import ToolErrorCode
from retail_analytics.domain.runs import (
    Run,
    RunStatus,
    WorkflowRef,
)


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
