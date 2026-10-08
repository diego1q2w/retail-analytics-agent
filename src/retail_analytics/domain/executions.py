"""Durable records of tool executions (operations) within a run.

One operation has one common record whose status is authoritative, an
append-only transition history, and optional source-specific detail such as a
warehouse job reference. The operation ID is application-generated, stable
across retries and resumption, and doubles as the idempotency key.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
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
    # RETRYING from PREPARED records a transient failure before submission
    # (for example a dry run or catalog read), when no external effect exists.
    _S.PREPARED: frozenset({_S.SUBMITTING, _S.RUNNING, _S.RETRYING}) | _ENDINGS,
    _S.SUBMITTING: frozenset({_S.RUNNING, _S.OUTCOME_UNKNOWN, _S.RETRYING}) | _ENDINGS,
    _S.RUNNING: frozenset({_S.OUTCOME_UNKNOWN, _S.RETRYING}) | _ENDINGS,
    # Reconciliation either finds the effect (running/finished) or proves there
    # is none, after which the operation may be submitted again.
    _S.OUTCOME_UNKNOWN: frozenset({_S.SUBMITTING, _S.RUNNING, _S.RETRYING}) | _ENDINGS,
    # A later attempt may fail transiently again before submitting anything.
    _S.RETRYING: frozenset({_S.SUBMITTING, _S.RUNNING, _S.RETRYING}) | _ENDINGS,
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
    # 1 for the first job of the operation; a new submission (and job ID) is
    # only made once the previous job is known to have ended without a result.
    submission: int = 1

    def __post_init__(self) -> None:
        if self.submission < 1:
            raise ValueError("submission starts at 1")


_JOB_NAMESPACE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def query_job_id(namespace: str, operation_id: str, submission: int) -> str:
    """Deterministic warehouse job ID of one submission of an operation.

    Resubmitting the same submission reuses the same ID, so the warehouse
    rejects a duplicate instead of running the query twice. The operation ID
    is hashed: job IDs are visible to every project principal.
    """
    if not _JOB_NAMESPACE.match(namespace):
        raise ValueError("invalid job namespace")
    if submission < 1:
        raise ValueError("submission starts at 1")
    digest = hashlib.sha256(operation_id.encode()).hexdigest()[:40]
    return f"{namespace}_{digest}_{submission}"


def transient_failures(history: Sequence[ExecutionEvent]) -> int:
    """Attempts of an operation that ended in a transient failure.

    Counted from the persisted transition history (a ``RETRYING`` transition
    carrying an error code), so resumption or a new worker cannot reset it.
    A resubmission notice without an error is not a failure.
    """
    return len(
        {
            event.attempt
            for event in history
            if event.to_status is ToolExecutionStatus.RETRYING
            and event.error_code is not None
        }
    )
