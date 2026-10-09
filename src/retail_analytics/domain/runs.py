"""Investigation runs: one user request and everything done to answer it.

A run's application ID is distinct from the Temporal workflow identifiers that
execute it; those are attached once the workflow is started.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from retail_analytics.domain.errors import InvalidTransition


class RunStatus(StrEnum):
    RUNNING = "running"
    # Paused on a clarification; silence never resumes it.
    WAITING_FOR_INPUT = "waiting_for_input"
    # Cancellation requested; in-flight effects are being reconciled.
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_active(self) -> bool:
        return self in ACTIVE_RUN_STATUSES

    @property
    def is_terminal(self) -> bool:
        return not self.is_active


# A session has at most one run in these states.
ACTIVE_RUN_STATUSES = frozenset(
    {RunStatus.RUNNING, RunStatus.WAITING_FOR_INPUT, RunStatus.CANCELLING}
)

_RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.WAITING_FOR_INPUT,
            RunStatus.CANCELLING,
            RunStatus.COMPLETED,
            RunStatus.PARTIAL,
            RunStatus.FAILED,
        }
    ),
    RunStatus.WAITING_FOR_INPUT: frozenset(
        {
            RunStatus.RUNNING,
            RunStatus.CANCELLING,
            # Nothing is in flight while waiting, so expiry/cancel can end it.
            RunStatus.CANCELLED,
            RunStatus.FAILED,
        }
    ),
    RunStatus.CANCELLING: frozenset(
        {RunStatus.CANCELLED, RunStatus.PARTIAL, RunStatus.FAILED}
    ),
}


@dataclass(frozen=True, slots=True)
class WorkflowRef:
    """Execution-runtime identifiers of a run (a Temporal workflow ID and
    first run ID today). Plain strings: no runtime handle is stored."""

    workflow_id: str
    workflow_run_id: str | None = None


@dataclass(frozen=True, slots=True)
class Run:
    run_id: str
    session_id: str
    requested_by: str
    trigger_message_id: str
    # Client-supplied key: resubmitting the same request returns this run.
    submission_key: str
    status: RunStatus
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    workflow: WorkflowRef | None = None

    def transition(self, to: RunStatus, *, at: datetime) -> Run | None:
        """The run after moving to ``to``; ``None`` when it is already there."""
        if to is self.status:
            return None
        if to not in _RUN_TRANSITIONS.get(self.status, frozenset()):
            raise InvalidTransition("run", self.status, to)
        return replace(
            self,
            status=to,
            updated_at=at,
            completed_at=at if to.is_terminal else None,
        )
