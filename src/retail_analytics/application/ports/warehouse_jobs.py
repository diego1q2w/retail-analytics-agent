from __future__ import annotations

from typing import Protocol

from retail_analytics.application.contracts.result_privacy import QueryRows
from retail_analytics.application.contracts.warehouse_jobs import (
    JobRef,
    JobSnapshot,
    JobSubmission,
)


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
