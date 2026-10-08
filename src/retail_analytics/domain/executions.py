"""Durable records of tool executions (operations) within a run.

One operation has one common record whose status is authoritative, an
append-only transition history, and optional source-specific detail such as a
warehouse job reference. The operation ID is application-generated, stable
across retries and resumption, and doubles as the idempotency key.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from retail_analytics.domain.errors import InvalidTransition
from retail_analytics.domain.operations import SideEffect, ToolErrorCode

MAX_DETAIL_LENGTH = 280


class ToolExecutionStatus(StrEnum):
    # Recorded, nothing submitted yet.
    PREPARED = "prepared"
    # About to submit an external effect whose reference is already recorded.
    SUBMITTING = "submitting"
    RUNNING = "running"
    # An external effect may exist; reconcile before any resubmission.
    OUTCOME_UNKNOWN = "outcome_unknown"
    # A transient failure; the next attempt of the same operation follows.
    RETRYING = "retrying"
    CANCEL_REQUESTED = "cancel_requested"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL


_TERMINAL = frozenset(
    {
        ToolExecutionStatus.SUCCEEDED,
        ToolExecutionStatus.FAILED,
        ToolExecutionStatus.CANCELLED,
    }
)
_S = ToolExecutionStatus
_ENDINGS = frozenset({_S.SUCCEEDED, _S.FAILED, _S.CANCEL_REQUESTED, _S.CANCELLED})
_TRANSITIONS: dict[ToolExecutionStatus, frozenset[ToolExecutionStatus]] = {
    _S.PREPARED: frozenset({_S.SUBMITTING, _S.RUNNING}) | _ENDINGS,
    _S.SUBMITTING: frozenset({_S.RUNNING, _S.OUTCOME_UNKNOWN, _S.RETRYING}) | _ENDINGS,
    _S.RUNNING: frozenset({_S.OUTCOME_UNKNOWN, _S.RETRYING}) | _ENDINGS,
    # Reconciliation either finds the effect (running/finished) or proves there
    # is none, after which the operation may be submitted again.
    _S.OUTCOME_UNKNOWN: frozenset({_S.SUBMITTING, _S.RUNNING, _S.RETRYING}) | _ENDINGS,
    _S.RETRYING: frozenset({_S.SUBMITTING, _S.RUNNING}) | _ENDINGS,
    _S.CANCEL_REQUESTED: frozenset(
        {_S.OUTCOME_UNKNOWN, _S.SUCCEEDED, _S.FAILED, _S.CANCELLED}
    ),
}
# Statuses that may carry an error code; FAILED requires one.
_ERROR_STATUSES = frozenset({_S.FAILED, _S.RETRYING, _S.OUTCOME_UNKNOWN})


@dataclass(frozen=True, slots=True)
class ExecutionEvent:
    """One accepted transition of an operation, in per-operation order."""

    operation_id: str
    sequence: int
    from_status: ToolExecutionStatus | None
    to_status: ToolExecutionStatus
    attempt: int
    occurred_at: datetime
    error_code: ToolErrorCode | None = None
    # Sanitized, fixed-vocabulary text; never raw data values or SQL.
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ToolExecution:
    operation_id: str
    run_id: str
    capability: str
    capability_version: int
    side_effect: SideEffect
    status: ToolExecutionStatus
    # Highest attempt started; 0 while only prepared.
    attempt_count: int
    created_at: datetime
    updated_at: datetime
    deadline_at: datetime | None = None
    error_code: ToolErrorCode | None = None
    error_detail: str | None = None

    def transition(
        self,
        to: ToolExecutionStatus,
        *,
        attempt: int,
        at: datetime,
        error_code: ToolErrorCode | None = None,
        detail: str | None = None,
    ) -> ToolExecution | None:
        """Apply a transition reported by attempt ``attempt``.

        Returns ``None`` when the operation is already in ``to`` for that
        attempt, so a retried activity that reports the same fact twice records
        it once. Reports from an older attempt are rejected.
        """
        if attempt < max(self.attempt_count, 1):
            raise InvalidTransition("tool execution", self.status, f"{to}@{attempt}")
        if to is self.status and attempt == self.attempt_count:
            return None
        if to not in _TRANSITIONS.get(self.status, frozenset()):
            raise InvalidTransition("tool execution", self.status, to)
        if (to is _S.FAILED) and error_code is None:
            raise ValueError("a failed operation needs an error code")
        if error_code is not None and to not in _ERROR_STATUSES:
            raise ValueError(f"{to} does not carry an error code")
        if detail is not None and len(detail) > MAX_DETAIL_LENGTH:
            raise ValueError("detail is too long")
        return replace(
            self,
            status=to,
            attempt_count=attempt,
            updated_at=at,
            error_code=error_code if to in _ERROR_STATUSES else self.error_code,
            error_detail=detail if to in _ERROR_STATUSES else self.error_detail,
        )


@dataclass(frozen=True, slots=True)
class QueryJob:
    """Warehouse job reference, recorded before the job is submitted.

    The job ID is generated by the backend so a lost submission response can be
    reconciled by looking the job up instead of submitting again.
    """

    operation_id: str
    job_id: str
    project: str
    location: str
    query_fingerprint: str
    # Reference to the protected compiled SQL and parameters, not the text.
    query_ref: str
    authorization_version: int
    catalog_version: str
