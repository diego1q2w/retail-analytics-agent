"""Vocabulary for agent-invoked operations: error codes and effect behaviour.

These codes are persisted on operation records and returned to the model, so
values are stable strings. Add a value rather than renaming one.
"""

from __future__ import annotations

from enum import StrEnum


class ToolErrorCode(StrEnum):
    """Why an operation failed. Pending and unknown outcomes are not errors."""

    INVALID_INPUT = "INVALID_INPUT"
    INVALID_QUERY = "INVALID_QUERY"
    UNSUPPORTED_SQL = "UNSUPPORTED_SQL"
    ACCESS_DENIED = "ACCESS_DENIED"
    FIELD_UNAVAILABLE = "FIELD_UNAVAILABLE"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    TEMPORARY_FAILURE = "TEMPORARY_FAILURE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class SideEffect(StrEnum):
    """What executing a capability can change outside the process."""

    # No state change; repeating it is harmless apart from cost.
    READ_ONLY = "read_only"
    # Application-owned state change made safe to repeat by the operation key.
    IDEMPOTENT_WRITE = "idempotent_write"
    # A billable or long-running external job (e.g. a warehouse query) that may
    # exist after a timeout and must be looked up by reference before resubmitting.
    EXTERNAL_JOB = "external_job"
    # An irreversible external effect (e.g. sending a message); never resend blindly.
    EXTERNAL_DELIVERY = "external_delivery"

    @property
    def may_leave_external_effect(self) -> bool:
        return self in {SideEffect.EXTERNAL_JOB, SideEffect.EXTERNAL_DELIVERY}


class RecoveryMode(StrEnum):
    """How the backend may recover a failed attempt of one capability."""

    # Retry transient failures with backoff within the run budget.
    RETRY = "retry"
    # Reconcile the recorded external reference first; resubmit only when it is
    # known that no effect exists.
    RECONCILE_FIRST = "reconcile_first"
    # Single attempt; failures are surfaced, never retried automatically.
    NO_RETRY = "no_retry"
