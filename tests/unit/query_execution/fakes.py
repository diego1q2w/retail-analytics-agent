"""In-memory stand-ins for query execution: records, warehouse and authority.

The repositories follow the PostgreSQL adapters' contract (idempotent begin,
domain-checked transitions, consecutive job submissions). The warehouse runs
compiled statements on the DuckDB oracle and lets tests inject the faults a
real warehouse produces: lost responses, crashes, slow jobs, missing jobs,
quota and transient failures.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

import duckdb
import sqlglot

from retail_analytics.application.authorization import AccessDenied, Principal
from retail_analytics.application.contracts import Correlation
from retail_analytics.application.discovery import CatalogUnavailable
from retail_analytics.application.persistence import (
    IdempotencyConflict,
    OperationRequest,
    OperationStart,
    RecordNotFound,
)
from retail_analytics.application.query_execution import QueryAuthority
from retail_analytics.application.result_privacy import QueryRows
from retail_analytics.application.tools.context import ExecutionContext
from retail_analytics.application.warehouse_jobs import (
    JobAlreadyExists,
    JobRef,
    JobSnapshot,
    JobState,
    JobStatistics,
    JobSubmission,
    SubmissionRejected,
    WarehouseUnavailable,
)
from retail_analytics.domain.access import Permission, ProductScope
from retail_analytics.domain.executions import (
    ExecutionEvent,
    QueryJob,
    ToolExecution,
    ToolExecutionStatus,
)
from retail_analytics.domain.operations import ToolErrorCode
from tests.unit.sql_compiler.support import view

T0 = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


class MemoryOperations:
    def __init__(self) -> None:
        self.records: dict[str, ToolExecution] = {}
        self.events: dict[str, list[ExecutionEvent]] = {}

    async def begin(self, request: OperationRequest) -> OperationStart:
        existing = self.records.get(request.operation_id)
        if existing is not None:
            if existing.run_id != request.run_id:
                raise IdempotencyConflict("operation", request.operation_id)
            return OperationStart(existing, created=False)
        op = ToolExecution(
            operation_id=request.operation_id,
            run_id=request.run_id,
            capability=request.capability,
            capability_version=request.capability_version,
            side_effect=request.side_effect,
            status=ToolExecutionStatus.PREPARED,
            attempt_count=0,
            created_at=T0,
            updated_at=T0,
        )
        self.records[op.operation_id] = op
        self.events[op.operation_id] = [
            ExecutionEvent(op.operation_id, 1, None, op.status, 0, T0)
        ]
        return OperationStart(op, created=True)

    async def get(self, operation_id: str) -> ToolExecution | None:
        return self.records.get(operation_id)

    async def transition(
        self,
        operation_id: str,
        to: ToolExecutionStatus,
        *,
        attempt: int,
        error_code: ToolErrorCode | None = None,
        detail: str | None = None,
    ) -> ToolExecution:
        current = self.records.get(operation_id)
        if current is None:
            raise RecordNotFound("operation", operation_id)
        updated = current.transition(
            to, attempt=attempt, at=T0, error_code=error_code, detail=detail
        )
        if updated is None:
            return current
        self.records[operation_id] = updated
        history = self.events[operation_id]
        history.append(
            ExecutionEvent(
                operation_id,
                len(history) + 1,
                current.status,
                to,
                attempt,
                T0,
                error_code,
                detail,
            )
        )
        return updated

    async def history(self, operation_id: str) -> Sequence[ExecutionEvent]:
        return list(self.events.get(operation_id, []))

    async def for_run(self, run_id: str) -> Sequence[ToolExecution]:
        return [r for r in self.records.values() if r.run_id == run_id]

    def statuses(self, operation_id: str) -> list[ToolExecutionStatus]:
        return [e.to_status for e in self.events[operation_id]]


class MemoryQueryJobs:
    def __init__(self, operations: MemoryOperations) -> None:
        self._operations = operations
        self.rows: dict[str, list[QueryJob]] = {}

    async def register_job(self, job: QueryJob) -> QueryJob:
        if job.operation_id not in self._operations.records:
            raise RecordNotFound("operation", job.operation_id)
        jobs = self.rows.setdefault(job.operation_id, [])
        for existing in jobs:
            if existing.submission == job.submission:
                if existing != job:
                    raise IdempotencyConflict("query job", job.operation_id)
                return existing
        if job.submission != len(jobs) + 1:
            raise IdempotencyConflict("query job", job.operation_id)
        if any(j.job_id == job.job_id for rows in self.rows.values() for j in rows):
            raise IdempotencyConflict("query job", job.job_id)
        jobs.append(job)
        return job

    async def get_job(self, operation_id: str) -> QueryJob | None:
        jobs = self.rows.get(operation_id)
        return jobs[-1] if jobs else None

    async def jobs(self, operation_id: str) -> Sequence[QueryJob]:
        return list(self.rows.get(operation_id, []))


class CrashAfterSubmit(Exception):
    """The process died right after the warehouse accepted the job."""


@dataclass
class FakeJob:
    submission: JobSubmission
    # Lookups that still report the job as running before it finishes.
    remaining_polls: int
    error_reason: str | None
    state: JobState = JobState.RUNNING
    cancelled: bool = False


def oracle_runner(
    db: duckdb.DuckDBPyConnection,
) -> Callable[[JobSubmission], QueryRows]:
    def run(submission: JobSubmission) -> QueryRows:
        (sql,) = sqlglot.transpile(submission.sql, read="bigquery", write="duckdb")
        params = {
            p.name: list(p.value) if isinstance(p.value, tuple) else p.value
            for p in submission.parameters
            if f"${p.name}" in sql
        }
        cursor = db.execute(sql, params)
        return QueryRows(tuple(d[0] for d in cursor.description), cursor.fetchall())

    return run


@dataclass
class FakeWarehouse:
    """Warehouse with caller-chosen job IDs and injectable faults."""

    runner: Callable[[JobSubmission], QueryRows]
    jobs: dict[str, FakeJob] = field(default_factory=dict)
    # Faults applied to the next calls, in order: "lost_response",
    # "unavailable", "crash", or "rejected:<reason>".
    submit_faults: list[str] = field(default_factory=list)
    # Final outcome of the next created jobs: None = success, else a reason.
    job_errors: list[str | None] = field(default_factory=list)
    lookup_faults: list[str] = field(default_factory=list)
    dry_run_faults: list[str] = field(default_factory=list)
    fetch_faults: list[str] = field(default_factory=list)
    polls_to_finish: int = 0
    estimated_bytes: int = 1024
    created: list[str] = field(default_factory=list)
    submit_calls: int = 0
    dry_runs: list[JobSubmission] = field(default_factory=list)
    cancels: list[str] = field(default_factory=list)
    before_submit: Callable[[JobSubmission], None] | None = None
    after_finish: Callable[[], None] | None = None

    async def dry_run(self, submission: JobSubmission) -> int:
        self.dry_runs.append(submission)
        if self.dry_run_faults:
            fault = self.dry_run_faults.pop(0)
            if fault == "unavailable":
                raise WarehouseUnavailable("unavailable")
            raise SubmissionRejected(fault.removeprefix("rejected:"))
        return self.estimated_bytes

    async def submit(self, submission: JobSubmission) -> JobSnapshot:
        self.submit_calls += 1
        if self.before_submit is not None:
            self.before_submit(submission)
        fault = self.submit_faults.pop(0) if self.submit_faults else None
        if fault == "unavailable":
            raise WarehouseUnavailable("unavailable")
        if fault is not None and fault.startswith("rejected:"):
            raise SubmissionRejected(fault.removeprefix("rejected:"))
        job_id = submission.ref.job_id
        if job_id in self.jobs:
            raise JobAlreadyExists("duplicate")
        error = self.job_errors.pop(0) if self.job_errors else None
        self.jobs[job_id] = FakeJob(submission, self.polls_to_finish, error)
        self.created.append(job_id)
        if fault == "lost_response":
            raise WarehouseUnavailable("timeout_unconfirmed")
        if fault == "crash":
            raise CrashAfterSubmit
        return self._snapshot(job_id)

    async def lookup(self, ref: JobRef) -> JobSnapshot | None:
        if self.lookup_faults:
            self.lookup_faults.pop(0)
            raise WarehouseUnavailable("unavailable")
        job = self.jobs.get(ref.job_id)
        if job is None:
            return None
        if job.state is JobState.RUNNING:
            if job.remaining_polls > 0:
                job.remaining_polls -= 1
            else:
                job.state = JobState.DONE
                if self.after_finish is not None:
                    self.after_finish()
        return self._snapshot(ref.job_id)

    async def fetch_rows(self, ref: JobRef, *, max_rows: int) -> QueryRows:
        if self.fetch_faults:
            self.fetch_faults.pop(0)
            raise WarehouseUnavailable("unavailable")
        job = self.jobs[ref.job_id]
        assert job.state is JobState.DONE and job.error_reason is None
        result = self.runner(job.submission)
        rows = list(result.rows)
        return QueryRows(result.columns, rows[:max_rows], len(rows) <= max_rows)

    async def cancel(self, ref: JobRef) -> None:
        self.cancels.append(ref.job_id)
        job = self.jobs.get(ref.job_id)
        if job is not None and job.state is not JobState.DONE:
            job.state = JobState.DONE
            job.cancelled = True
            job.error_reason = "stopped"

    def finish(self, job_id: str) -> None:
        self.jobs[job_id].remaining_polls = 0

    def _snapshot(self, job_id: str) -> JobSnapshot:
        job = self.jobs[job_id]
        done = job.state is JobState.DONE
        return JobSnapshot(
            ref=job.submission.ref,
            state=job.state,
            fingerprint=job.submission.fingerprint,
            error_reason=job.error_reason if done else None,
            statistics=JobStatistics(
                bytes_processed=self.estimated_bytes if done else None,
                bytes_billed=10 * 1024 * 1024 if done else None,
                cache_hit=False,
            ),
        )


@dataclass
class FakeAuthority:
    """Current authority, mutable between calls; counts every resolution."""

    executive_id: str
    scope: ProductScope
    permissions: frozenset[str] = frozenset({Permission.ANALYSIS_READ.value})
    deny: bool = False
    catalog_down: bool = False
    resolutions: int = 0

    async def resolve(
        self, principal: Principal, run_id: str, *, trace_id: str | None = None
    ) -> QueryAuthority:
        self.resolutions += 1
        if self.deny or principal.executive_id != self.executive_id:
            raise AccessDenied("run", run_id)
        if self.catalog_down:
            raise CatalogUnavailable("schema metadata is temporarily unavailable")
        context = ExecutionContext(
            executive_id=self.executive_id,
            permissions=self.permissions,
            product_scope=self.scope,
            correlation=Correlation(session_id="ses-1", run_id=run_id),
        )
        return QueryAuthority(context, view(version=self.scope.entitlement_version))

    def change_scope(self, *products: int) -> None:
        self.scope = ProductScope(
            frozenset(str(p) for p in products), self.scope.entitlement_version + 1
        )
