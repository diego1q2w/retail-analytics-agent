"""Port to a warehouse that runs read-only query jobs with caller-chosen IDs.

SDK-free: the BigQuery adapter implements it, the query execution service
uses it, and fakes in tests obey the same contract. Failure reasons are a
fixed vocabulary (``REASONS``) so they can be persisted and shown without
carrying provider messages, SQL or values.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from retail_analytics.application.query_compiler import QueryParameter
from retail_analytics.application.result_privacy import QueryRows
from retail_analytics.domain.executions import QueryJob
from retail_analytics.domain.operations import ToolErrorCode

FINGERPRINT_LABEL = "ra_fingerprint"


@dataclass(frozen=True, slots=True)
class JobRef:
    project: str
    location: str
    job_id: str

    @classmethod
    def of(cls, job: QueryJob) -> JobRef:
        return cls(job.project, job.location, job.job_id)


class JobState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"


@dataclass(frozen=True, slots=True)
class JobStatistics:
    bytes_processed: int | None = None
    bytes_billed: int | None = None
    cache_hit: bool | None = None


@dataclass(frozen=True, slots=True)
class JobSnapshot:
    """What the warehouse reports about one job. No SQL, values or messages."""

    ref: JobRef
    state: JobState
    # The fingerprint label the job was submitted with (None if absent).
    fingerprint: str | None
    # Sanitized warehouse failure reason of a finished job (see ``REASONS``).
    error_reason: str | None = None
    statistics: JobStatistics = JobStatistics()

    @property
    def succeeded(self) -> bool:
        return self.state is JobState.DONE and self.error_reason is None


@dataclass(frozen=True, slots=True)
class JobSubmission:
    """A statement ready for the warehouse. Never rendered in logs."""

    ref: JobRef
    sql: str
    parameters: tuple[QueryParameter, ...]
    maximum_bytes_billed: int
    fingerprint: str
    # The warehouse stops the job by itself after this long (a backstop to
    # the application's own deadline). None: no job timeout.
    timeout_seconds: float | None = None

    def __repr__(self) -> str:
        return f"JobSubmission(ref={self.ref!r}, fingerprint={self.fingerprint!r})"


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


class WarehouseQueryJobs(Protocol):
    """Read-only query jobs with caller-chosen IDs. Implementations must:

    - submit with exactly ``ref.job_id`` and never generate another ID or
      resubmit on their own;
    - set ``maximum_bytes_billed`` and the fingerprint label on the job;
    - raise the port errors above, never SDK exceptions or provider messages.
    """

    async def dry_run(self, submission: JobSubmission) -> int:
        """Estimated bytes processed. Creates no job."""
        ...

    async def submit(self, submission: JobSubmission) -> JobSnapshot: ...

    async def lookup(self, ref: JobRef) -> JobSnapshot | None:
        """The job, or ``None`` when the warehouse has no job with that ID."""
        ...

    async def fetch_rows(self, ref: JobRef, *, max_rows: int) -> QueryRows:
        """Up to ``max_rows`` rows of a successful job; ``complete`` is honest."""
        ...

    async def cancel(self, ref: JobRef) -> None:
        """Request cancellation; a missing or finished job is not an error."""
        ...


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
