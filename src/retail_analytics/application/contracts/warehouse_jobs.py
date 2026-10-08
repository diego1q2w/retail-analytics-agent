from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from retail_analytics.application.contracts.query_compiler import QueryParameter
from retail_analytics.domain.executions import QueryJob


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
