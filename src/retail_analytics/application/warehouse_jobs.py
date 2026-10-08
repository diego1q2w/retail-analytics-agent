"""Port to a warehouse that runs read-only query jobs with caller-chosen IDs.

SDK-free: the BigQuery adapter implements it, the query execution service
uses it, and fakes in tests obey the same contract. Failure reasons are a
fixed vocabulary (``REASONS``) so they can be persisted and shown without
carrying provider messages, SQL or values.
"""

from __future__ import annotations

from dataclasses import dataclass

from retail_analytics.domain.operations import ToolErrorCode

FINGERPRINT_LABEL = "ra_fingerprint"


class WarehouseError(Exception):
    """Base of warehouse port errors; ``reason`` is from ``REASONS``."""

    def __init__(self, reason: str) -> None:
        self.reason = sanitize_reason(reason)
        super().__init__(self.reason)


class WarehouseUnavailable(WarehouseError):
    """The call failed transiently; a submission may or may not have happened."""


class SubmissionRejected(WarehouseError):
    """The warehouse definitively refused the request; no job was created."""


class JobAlreadyExists(WarehouseError):
    """A job with this ID already exists (an earlier submission got through)."""


# Fixed vocabulary of warehouse reasons that may be persisted or shown.
_RETRYABLE = frozenset(
    {
        "backendError",
        "internalError",
        "jobBackendError",
        "jobInternalError",
        "rateLimitExceeded",
        "stopped",
        "unavailable",
        "timeout_unconfirmed",
    }
)
_BUDGET = frozenset(
    {
        "bytesBilledLimitExceeded",
        "billingTierLimitExceeded",
        "quotaExceeded",
        "resourcesExceeded",
        "responseTooLarge",
        "timeout",
    }
)
_INVALID = frozenset({"invalidQuery", "invalid"})
_DENIED = frozenset({"accessDenied"})
# Not retryable: a missing job/result, a duplicate ID, unusable credentials.
_OTHER = frozenset({"notFound", "duplicate", "credentials", "other"})
REASONS = _RETRYABLE | _BUDGET | _INVALID | _DENIED | _OTHER


def sanitize_reason(raw: str | None) -> str:
    return raw if raw in REASONS else "other"


@dataclass(frozen=True, slots=True)
class FailureClass:
    code: ToolErrorCode
    retryable: bool


def classify_reason(reason: str) -> FailureClass:
    """How a warehouse failure reason affects the operation."""
    if reason in _RETRYABLE:
        return FailureClass(ToolErrorCode.TEMPORARY_FAILURE, True)
    if reason in _BUDGET:
        return FailureClass(ToolErrorCode.BUDGET_EXCEEDED, False)
    if reason in _INVALID:
        return FailureClass(ToolErrorCode.INVALID_QUERY, False)
    if reason in _DENIED:
        return FailureClass(ToolErrorCode.ACCESS_DENIED, False)
    return FailureClass(ToolErrorCode.INTERNAL_ERROR, False)
