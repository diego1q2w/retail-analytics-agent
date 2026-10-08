"""Durable execution of analytical queries on the warehouse.

One query operation runs as one or more attempts (activity retries, worker
restarts, resumption). Every attempt:

1. re-resolves the executive's authority and catalog view and recompiles the
   model's query with them, so revoked access or changed definitions apply to
   work already in flight;
2. reconciles first: if a job reference is recorded it looks the job up and
   follows it instead of submitting, because the job may exist even when its
   submission response was lost;
3. otherwise dry-runs the statement, records the job reference (deterministic
   job ID derived from the operation ID and submission number) and only then
   submits it, so a crash at any point leaves a reference to reconcile;
4. waits a bounded time, and on success reads at most the releasable rows and
   passes them through :class:`ResultPrivacyBoundary` with authority resolved
   again, before anything is returned.

A second submission of the same operation is only made once the previous job
is known to have ended without a releasable result (transient job failure or
authority changed since submission). Resubmitting the *same* submission reuses
its job ID, which the warehouse rejects as a duplicate instead of running it
twice.

Run budgets, retry counts, backoff and the query deadline belong to the caller
(:class:`QueryAdmission` is the pre-submission seam). This module never logs
SQL or parameter values; failure details are a fixed vocabulary.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol

from retail_analytics.application.authorization import (
    AccessDenied,
    AccessResolver,
    Principal,
)
from retail_analytics.application.discovery import CatalogUnavailable, DiscoveryService
from retail_analytics.application.persistence import (
    OperationRequest,
    QueryJobRepository,
    ToolExecutionRepository,
)
from retail_analytics.application.query_compiler import (
    AnalysisQuery,
    CompiledQuery,
    QueryRejected,
    ScopedQueryCompilers,
)
from retail_analytics.application.result_privacy import (
    DEFAULT_MAX_ROWS,
    ReleasedResult,
    ResultPrivacyBoundary,
    ResultWithheld,
)
from retail_analytics.application.tools.context import ExecutionContext
from retail_analytics.application.warehouse_jobs import (
    JobAlreadyExists,
    JobRef,
    JobSnapshot,
    JobState,
    JobStatistics,
    JobSubmission,
    SubmissionRejected,
    WarehouseError,
    WarehouseQueryJobs,
    WarehouseUnavailable,
    classify_reason,
)
from retail_analytics.domain.access import Permission
from retail_analytics.domain.catalog import CatalogView
from retail_analytics.domain.errors import InvalidTransition
from retail_analytics.domain.executions import (
    QueryJob,
    ToolExecution,
    ToolExecutionStatus,
    query_job_id,
)
from retail_analytics.domain.operations import SideEffect, ToolErrorCode

QUERY_CAPABILITY = "run_query"
QUERY_CAPABILITY_VERSION = 1

_S = ToolExecutionStatus

# ---------------------------------------------------------------------------
# Authority and admission ports


@dataclass(frozen=True, slots=True)
class QueryAuthority:
    """Authority resolved for one attempt: never cached across attempts."""

    context: ExecutionContext
    catalog: CatalogView


class AuthorityProvider(Protocol):
    async def resolve(
        self, principal: Principal, run_id: str, *, trace_id: str | None = None
    ) -> QueryAuthority:
        """Raises ``AccessDenied`` or ``CatalogUnavailable``."""
        ...


class FreshQueryAuthority:
    """Current entitlements and catalog view, re-read on every call."""

    def __init__(self, resolver: AccessResolver, discovery: DiscoveryService) -> None:
        self._resolver = resolver
        self._discovery = discovery

    async def resolve(
        self, principal: Principal, run_id: str, *, trace_id: str | None = None
    ) -> QueryAuthority:
        context = await self._resolver.context_for_run(
            principal, run_id, trace_id=trace_id
        )
        if Permission.ANALYSIS_READ not in context.permissions:
            raise AccessDenied("permission", Permission.ANALYSIS_READ.value)
        view = await self._discovery.view_for(context)
        return QueryAuthority(context, view.catalog)


class QueryNotAdmitted(Exception):
    """A pre-submission policy (e.g. run budget) refused the submission."""

    def __init__(self, code: ToolErrorCode, reason: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.reason = reason
        self.message = message


class QueryAdmission(Protocol):
    """Decides, before a job reference is recorded, whether it may be submitted.

    Called once per new submission (operation ID, submission number) with the
    dry-run estimate; a retried attempt may call it again for the same pair,
    so implementations must be idempotent on it. Raises ``QueryNotAdmitted``.
    """

    async def admit(
        self,
        context: ExecutionContext,
        operation_id: str,
        submission: int,
        compiled: CompiledQuery,
        estimated_bytes: int,
    ) -> None: ...


# ---------------------------------------------------------------------------
# Requests and outcomes


@dataclass(frozen=True, slots=True)
class QueryAttempt:
    """One attempt of one query operation, from trusted runtime state.

    ``query`` is the model's analytical query for the operation; it must be
    the same on every attempt (the operation ID identifies it).
    """

    principal: Principal
    run_id: str
    operation_id: str
    attempt: int
    query: AnalysisQuery
    trace_id: str | None = None

    def __post_init__(self) -> None:
        if self.attempt < 1:
            raise ValueError("attempt starts at 1")

    def __repr__(self) -> str:
        return (
            f"QueryAttempt(run_id={self.run_id!r}, operation_id="
            f"{self.operation_id!r}, attempt={self.attempt})"
        )


@dataclass(frozen=True, slots=True)
class QuerySucceeded:
    result: ReleasedResult
    # Provenance for evidence: use ``compiled.analysis_parameters`` only.
    compiled: CompiledQuery
    job: QueryJob
    statistics: JobStatistics


@dataclass(frozen=True, slots=True)
class QueryPending:
    """JOB_PENDING: the recorded job is queued or running; check it again."""

    job: QueryJob


@dataclass(frozen=True, slots=True)
class QueryOutcomeUnknown:
    """OUTCOME_UNKNOWN: reconcile ``job`` (if any) before anything else."""

    job: QueryJob | None
    reason: str


@dataclass(frozen=True, slots=True)
class QueryFailed:
    code: ToolErrorCode
    reason: str
    message: str
    # A later attempt of the same operation may succeed (after backoff).
    retryable: bool = False

    @property
    def correctable(self) -> bool:
        """The agent may reformulate the query as a new operation."""
        return self.code in (
            ToolErrorCode.INVALID_QUERY,
            ToolErrorCode.UNSUPPORTED_SQL,
            ToolErrorCode.INVALID_INPUT,
            ToolErrorCode.FIELD_UNAVAILABLE,
        )


@dataclass(frozen=True, slots=True)
class QueryCancelled:
    job: QueryJob | None
    # False while cancellation was requested but the job has not stopped yet.
    confirmed: bool


type QueryOutcome = (
    QuerySucceeded | QueryPending | QueryOutcomeUnknown | QueryFailed | QueryCancelled
)

_MESSAGES: dict[ToolErrorCode, str] = {
    ToolErrorCode.ACCESS_DENIED: "This data is not available to you.",
    ToolErrorCode.TEMPORARY_FAILURE: (
        "The warehouse is temporarily unavailable; the query has not produced a "
        "result yet."
    ),
    ToolErrorCode.BUDGET_EXCEEDED: (
        "The query exceeds its resource limits; narrow the period or the data read."
    ),
    ToolErrorCode.INVALID_QUERY: "The warehouse rejected the query; reformulate it.",
    ToolErrorCode.INTERNAL_ERROR: "The query failed and produced no result.",
}


def _failed(
    code: ToolErrorCode, reason: str, *, retryable: bool = False
) -> QueryFailed:
    return QueryFailed(code, reason, _MESSAGES[code], retryable)


# ---------------------------------------------------------------------------
# Fingerprint


def _canonical(value: object) -> object:
    if isinstance(value, tuple):
        return [_canonical(v) for v in value]
    if isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, Decimal | date):
        return str(value)
    raise TypeError(f"unsupported parameter value {type(value).__name__}")


def query_fingerprint(compiled: CompiledQuery) -> str:
    """Identity of the exact statement a job runs (SQL, parameters, byte cap).

    Secret parameter values are included so that a key change is a different
    statement; the digest does not reveal them.
    """
    body = json.dumps(
        [
            compiled.sql,
            compiled.maximum_bytes_billed,
            sorted(
                [p.name, p.type.value, p.array, _canonical(p.value)]
                for p in compiled.parameters
            ),
        ],
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode()).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Service


@dataclass(frozen=True, slots=True)
class QueryExecutionSettings:
    project: str
    location: str
    # Prefix of generated job IDs; separates deployments sharing a project.
    job_namespace: str = "ra"
    # How long one attempt follows a running job before returning pending.
    wait_seconds: float = 60.0
    poll_initial_seconds: float = 0.5
    poll_max_seconds: float = 5.0
    max_rows: int = DEFAULT_MAX_ROWS

    def __post_init__(self) -> None:
        if not self.project or not self.location:
            raise ValueError("project and location are required")
        query_job_id(self.job_namespace, "check", 1)
        if self.wait_seconds < 0 or self.poll_initial_seconds <= 0:
            raise ValueError("invalid wait settings")


class _Superseded(Exception):
    """A newer attempt already moved the operation on."""


class QueryExecutionService:
    def __init__(
        self,
        *,
        settings: QueryExecutionSettings,
        authority: AuthorityProvider,
        compilers: ScopedQueryCompilers,
        boundary: ResultPrivacyBoundary,
        warehouse: WarehouseQueryJobs,
        operations: ToolExecutionRepository,
        jobs: QueryJobRepository,
        admission: QueryAdmission | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._authority = authority
        self._compilers = compilers
        self._boundary = boundary
        self._warehouse = warehouse
        self._operations = operations
        self._jobs = jobs
        self._admission = admission
        self._sleep = sleep
        self._monotonic = monotonic

    # -- public use cases -------------------------------------------------

    async def execute(self, attempt: QueryAttempt) -> QueryOutcome:
        """Run or continue one attempt of the operation. Never raises for
        warehouse, authorization or policy outcomes; they become outcomes."""
        try:
            return await self._execute(attempt)
        except (_Superseded, InvalidTransition):
            job = await self._jobs.get_job(attempt.operation_id)
            return QueryOutcomeUnknown(job, "superseded_attempt")

    async def cancel(self, run_id: str, operation_id: str) -> QueryOutcome:
        """Stop the operation and reconcile its job.

        A trusted control path: the caller has already authorized cancelling
        the run. Returns ``QueryCancelled(confirmed=False)`` while the job is
        still stopping; call :meth:`reconcile_cancel` again later.
        """
        op = await self._operations.get(operation_id)
        if op is None or op.run_id != run_id:
            return _failed(ToolErrorCode.ACCESS_DENIED, "operation_not_in_run")
        if op.status.is_terminal:
            return await self._terminal_summary(op)
        job = await self._jobs.get_job(operation_id)
        attempt = max(op.attempt_count, 1)
        if job is None:
            await self._move(op, _S.CANCELLED, attempt, detail="cancelled")
            return QueryCancelled(None, confirmed=True)
        op = await self._move(op, _S.CANCEL_REQUESTED, attempt, detail="cancel")
        try:
            await self._warehouse.cancel(JobRef.of(job))
        except WarehouseError:
            return QueryCancelled(job, confirmed=False)
        return await self._reconcile_cancel(op, job, attempt)

    async def reconcile_cancel(self, run_id: str, operation_id: str) -> QueryOutcome:
        op = await self._operations.get(operation_id)
        if op is None or op.run_id != run_id:
            return _failed(ToolErrorCode.ACCESS_DENIED, "operation_not_in_run")
        if op.status is not _S.CANCEL_REQUESTED:
            return await self._terminal_summary(op)
        job = await self._jobs.get_job(operation_id)
        return await self._reconcile_cancel(op, job, max(op.attempt_count, 1))

    # -- attempt flow -----------------------------------------------------

    async def _execute(self, attempt: QueryAttempt) -> QueryOutcome:
        try:
            authority = await self._authority.resolve(
                attempt.principal, attempt.run_id, trace_id=attempt.trace_id
            )
        except AccessDenied:
            # Not owned and not permitted look alike; touch no records.
            return _failed(ToolErrorCode.ACCESS_DENIED, "access_denied")
        except CatalogUnavailable:
            return _failed(
                ToolErrorCode.TEMPORARY_FAILURE, "catalog_unavailable", retryable=True
            )

        op = await self._operation(attempt)
        if op is None:
            return _failed(ToolErrorCode.ACCESS_DENIED, "operation_not_in_run")
        if op.status is _S.CANCEL_REQUESTED:
            job = await self._jobs.get_job(op.operation_id)
            return await self._reconcile_cancel(op, job, max(op.attempt_count, 1))

        compiler = self._compilers.for_executive(authority.context.executive_id)
        job = await self._jobs.get_job(op.operation_id)
        try:
            compiled = compiler.compile(
                attempt.query,
                catalog=authority.catalog,
                scope=authority.context.product_scope,
            )
        except QueryRejected as rejected:
            if op.status.is_terminal:
                return QueryFailed(rejected.code, rejected.reason, rejected.message)
            await self._stop_job(job)
            await self._move(
                op,
                _S.FAILED,
                attempt.attempt,
                error_code=rejected.code,
                detail=f"compile_{rejected.reason}"[:64],
            )
            return QueryFailed(rejected.code, rejected.reason, rejected.message)
        fingerprint = query_fingerprint(compiled)

        if op.status.is_terminal:
            return await self._replay(op, job, compiled, fingerprint, attempt)
        if job is None:
            return await self._new_submission(
                op, attempt, authority, compiled, fingerprint, 1
            )
        return await self._reconcile(op, attempt, authority, compiled, fingerprint, job)

    async def _operation(self, attempt: QueryAttempt) -> ToolExecution | None:
        op = await self._operations.get(attempt.operation_id)
        if op is None:
            started = await self._operations.begin(
                OperationRequest(
                    operation_id=attempt.operation_id,
                    run_id=attempt.run_id,
                    capability=QUERY_CAPABILITY,
                    capability_version=QUERY_CAPABILITY_VERSION,
                    side_effect=SideEffect.EXTERNAL_JOB,
                )
            )
            op = started.execution
        if op.run_id != attempt.run_id:
            return None
        if attempt.attempt < op.attempt_count:
            raise _Superseded
        return op

    async def _reconcile(
        self,
        op: ToolExecution,
        attempt: QueryAttempt,
        authority: QueryAuthority,
        compiled: CompiledQuery,
        fingerprint: str,
        job: QueryJob,
    ) -> QueryOutcome:
        """A job reference exists: look it up before anything is submitted."""
        try:
            snapshot = await self._warehouse.lookup(JobRef.of(job))
        except WarehouseError:
            if op.status in (_S.SUBMITTING, _S.RUNNING):
                await self._move(
                    op, _S.OUTCOME_UNKNOWN, attempt.attempt, detail="lookup_failed"
                )
            return QueryOutcomeUnknown(job, "lookup_failed")

        if snapshot is None:
            # Never created (or not yet visible): resubmitting the same job ID
            # cannot create a duplicate, the warehouse would reject it.
            if job.query_fingerprint == fingerprint:
                return await self._submit(op, attempt, authority, compiled, job)
            return await self._new_submission(
                op, attempt, authority, compiled, fingerprint, job.submission + 1
            )
        if snapshot.fingerprint != job.query_fingerprint:
            await self._move(
                op,
                _S.FAILED,
                attempt.attempt,
                error_code=ToolErrorCode.INTERNAL_ERROR,
                detail="job_reference_conflict",
            )
            return _failed(ToolErrorCode.INTERNAL_ERROR, "job_reference_conflict")
        if job.query_fingerprint != fingerprint:
            # Authority or definitions changed since submission: the job's
            # result must never be released. Stop it and run the current query.
            if snapshot.state is not JobState.DONE:
                await self._stop_job(job)
            return await self._new_submission(
                op, attempt, authority, compiled, fingerprint, job.submission + 1
            )
        if (
            snapshot.state is JobState.DONE
            and snapshot.error_reason is not None
            and classify_reason(snapshot.error_reason).retryable
        ):
            return await self._new_submission(
                op, attempt, authority, compiled, fingerprint, job.submission + 1
            )
        return await self._follow(op, attempt, compiled, job, snapshot)

    async def _new_submission(
        self,
        op: ToolExecution,
        attempt: QueryAttempt,
        authority: QueryAuthority,
        compiled: CompiledQuery,
        fingerprint: str,
        submission: int,
    ) -> QueryOutcome:
        settings = self._settings
        job_id = query_job_id(settings.job_namespace, op.operation_id, submission)
        ref = JobRef(settings.project, settings.location, job_id)
        statement = _statement(ref, compiled, fingerprint)
        try:
            estimated = await self._warehouse.dry_run(statement)
        except WarehouseError as error:
            return await self._pre_submission_failure(
                op, attempt, f"dry_run_{error.reason}", error
            )
        if estimated > compiled.maximum_bytes_billed:
            await self._move(
                op,
                _S.FAILED,
                attempt.attempt,
                error_code=ToolErrorCode.BUDGET_EXCEEDED,
                detail="estimate_over_query_limit",
            )
            return _failed(ToolErrorCode.BUDGET_EXCEEDED, "estimate_over_query_limit")
        if self._admission is not None:
            try:
                await self._admission.admit(
                    authority.context, op.operation_id, submission, compiled, estimated
                )
            except QueryNotAdmitted as refused:
                await self._move(
                    op,
                    _S.FAILED,
                    attempt.attempt,
                    error_code=refused.code,
                    detail=f"not_admitted_{refused.reason}"[:64],
                )
                return QueryFailed(refused.code, refused.reason, refused.message)
        job = await self._jobs.register_job(
            QueryJob(
                operation_id=op.operation_id,
                job_id=job_id,
                project=ref.project,
                location=ref.location,
                query_fingerprint=fingerprint,
                query_ref=f"bigquery-job:{ref.project}/{ref.location}/{job_id}",
                authorization_version=compiled.entitlement_version,
                catalog_version=str(compiled.catalog_version),
                submission=submission,
            )
        )
        return await self._submit(op, attempt, authority, compiled, job)

    async def _submit(
        self,
        op: ToolExecution,
        attempt: QueryAttempt,
        authority: QueryAuthority,
        compiled: CompiledQuery,
        job: QueryJob,
    ) -> QueryOutcome:
        """Submit a recorded job reference (first time or after it was not found)."""
        n = attempt.attempt
        if op.status in (_S.SUBMITTING, _S.RUNNING):
            # An earlier attempt's job is gone or superseded; say so first.
            op = await self._move(op, _S.RETRYING, n, detail="resubmitting")
        op = await self._move(
            op, _S.SUBMITTING, n, detail=f"submission_{job.submission}"
        )
        ref = JobRef.of(job)
        try:
            snapshot = await self._warehouse.submit(
                _statement(ref, compiled, job.query_fingerprint)
            )
        except JobAlreadyExists:
            try:
                existing = await self._warehouse.lookup(ref)
            except WarehouseError:
                existing = None
            if existing is None:
                await self._move(op, _S.OUTCOME_UNKNOWN, n, detail="duplicate_unseen")
                return QueryOutcomeUnknown(job, "duplicate_unseen")
            if existing.fingerprint != job.query_fingerprint:
                await self._move(
                    op,
                    _S.FAILED,
                    n,
                    error_code=ToolErrorCode.INTERNAL_ERROR,
                    detail="job_reference_conflict",
                )
                return _failed(ToolErrorCode.INTERNAL_ERROR, "job_reference_conflict")
            snapshot = existing
        except SubmissionRejected as rejected:
            failure = classify_reason(rejected.reason)
            detail = f"submit_{rejected.reason}"
            if failure.retryable:
                await self._move(
                    op, _S.RETRYING, n, error_code=failure.code, detail=detail
                )
                return _failed(failure.code, detail, retryable=True)
            await self._move(op, _S.FAILED, n, error_code=failure.code, detail=detail)
            return _failed(failure.code, detail)
        except WarehouseUnavailable:
            await self._move(op, _S.OUTCOME_UNKNOWN, n, detail="submission_unconfirmed")
            return QueryOutcomeUnknown(job, "submission_unconfirmed")
        return await self._follow(op, attempt, compiled, job, snapshot)

    async def _follow(
        self,
        op: ToolExecution,
        attempt: QueryAttempt,
        compiled: CompiledQuery,
        job: QueryJob,
        snapshot: JobSnapshot,
    ) -> QueryOutcome:
        """Follow a job known to run the current statement until it ends."""
        n = attempt.attempt
        try:
            op = await self._move(op, _S.RUNNING, n)
        except InvalidTransition:
            # Cancelled or finished meanwhile by another path; never leave a
            # job we started running unobserved.
            await self._stop_job(job)
            raise
        done = await self._wait(job, snapshot)
        if done is None:
            return QueryPending(job)
        if done.error_reason is not None:
            failure = classify_reason(done.error_reason)
            detail = f"job_{done.error_reason}"
            if failure.retryable:
                await self._move(
                    op, _S.RETRYING, n, error_code=failure.code, detail=detail
                )
                return _failed(failure.code, detail, retryable=True)
            await self._move(op, _S.FAILED, n, error_code=failure.code, detail=detail)
            return _failed(failure.code, detail)
        outcome = await self._release(attempt, compiled, job, done)
        if isinstance(outcome, QuerySucceeded):
            await self._move(op, _S.SUCCEEDED, n)
        elif outcome.retryable:
            # The finished job is kept; the next attempt reads it again.
            await self._move(
                op, _S.RETRYING, n, error_code=outcome.code, detail=outcome.reason
            )
        else:
            await self._move(
                op, _S.FAILED, n, error_code=outcome.code, detail=outcome.reason[:64]
            )
        return outcome

    async def _release(
        self,
        attempt: QueryAttempt,
        compiled: CompiledQuery,
        job: QueryJob,
        snapshot: JobSnapshot,
    ) -> QuerySucceeded | QueryFailed:
        """Read the rows and release them under authority resolved right now."""
        try:
            rows = await self._warehouse.fetch_rows(
                JobRef.of(job), max_rows=self._settings.max_rows + 1
            )
        except WarehouseError as error:
            failure = classify_reason(error.reason)
            if isinstance(error, WarehouseUnavailable) or failure.retryable:
                return _failed(
                    ToolErrorCode.TEMPORARY_FAILURE,
                    "result_read_failed",
                    retryable=True,
                )
            return _failed(ToolErrorCode.INTERNAL_ERROR, "result_unavailable")
        try:
            current = await self._authority.resolve(
                attempt.principal, attempt.run_id, trace_id=attempt.trace_id
            )
        except AccessDenied:
            return _failed(ToolErrorCode.ACCESS_DENIED, "access_revoked")
        except CatalogUnavailable:
            return _failed(
                ToolErrorCode.TEMPORARY_FAILURE, "catalog_unavailable", retryable=True
            )
        if current.context.executive_id != attempt.principal.executive_id:
            return _failed(ToolErrorCode.ACCESS_DENIED, "access_revoked")
        try:
            released = self._boundary.release(compiled, rows, catalog=current.catalog)
        except ResultWithheld as withheld:
            return QueryFailed(
                withheld.code, f"withheld_{withheld.reason}"[:64], withheld.message
            )
        return QuerySucceeded(released, compiled, job, snapshot.statistics)

    async def _replay(
        self,
        op: ToolExecution,
        job: QueryJob | None,
        compiled: CompiledQuery,
        fingerprint: str,
        attempt: QueryAttempt,
    ) -> QueryOutcome:
        """A retried attempt of a finished operation: return the same outcome."""
        if op.status is not _S.SUCCEEDED or job is None:
            return await self._terminal_summary(op)
        if job.query_fingerprint != fingerprint:
            # The recorded result was computed under different authority or
            # definitions; it is not released. The agent can query again.
            return _failed(ToolErrorCode.ACCESS_DENIED, "authority_changed")
        try:
            snapshot = await self._warehouse.lookup(JobRef.of(job))
        except WarehouseError:
            snapshot = None
        statistics = JobStatistics() if snapshot is None else snapshot.statistics
        done = JobSnapshot(JobRef.of(job), JobState.DONE, fingerprint, None, statistics)
        return await self._release(attempt, compiled, job, done)

    async def _terminal_summary(self, op: ToolExecution) -> QueryOutcome:
        job = await self._jobs.get_job(op.operation_id)
        if op.status is _S.CANCELLED:
            return QueryCancelled(job, confirmed=True)
        if op.status is _S.SUCCEEDED:
            return _failed(ToolErrorCode.INTERNAL_ERROR, "already_succeeded")
        if op.status is _S.FAILED:
            code = op.error_code or ToolErrorCode.INTERNAL_ERROR
            return QueryFailed(
                code,
                op.error_detail or "failed",
                _MESSAGES.get(code, _MESSAGES[ToolErrorCode.INTERNAL_ERROR]),
            )
        if job is None:
            return QueryOutcomeUnknown(None, "not_finished")
        return QueryPending(job)

    async def _reconcile_cancel(
        self, op: ToolExecution, job: QueryJob | None, attempt: int
    ) -> QueryOutcome:
        if job is None:
            await self._move(op, _S.CANCELLED, attempt, detail="cancelled")
            return QueryCancelled(None, confirmed=True)
        try:
            snapshot = await self._warehouse.lookup(JobRef.of(job))
        except WarehouseError:
            return QueryCancelled(job, confirmed=False)
        if snapshot is not None and snapshot.state is not JobState.DONE:
            return QueryCancelled(job, confirmed=False)
        # Not found, stopped, failed or even finished: no result is released.
        await self._move(op, _S.CANCELLED, attempt, detail="cancelled")
        return QueryCancelled(job, confirmed=True)

    async def _pre_submission_failure(
        self,
        op: ToolExecution,
        attempt: QueryAttempt,
        detail: str,
        error: WarehouseError,
    ) -> QueryFailed:
        failure = classify_reason(error.reason)
        if isinstance(error, WarehouseUnavailable) or failure.retryable:
            await self._move(
                op,
                _S.RETRYING,
                attempt.attempt,
                error_code=ToolErrorCode.TEMPORARY_FAILURE,
                detail=detail[:64],
            )
            return _failed(ToolErrorCode.TEMPORARY_FAILURE, detail, retryable=True)
        await self._move(
            op, _S.FAILED, attempt.attempt, error_code=failure.code, detail=detail[:64]
        )
        return _failed(failure.code, detail)

    # -- helpers ----------------------------------------------------------

    async def _wait(self, job: QueryJob, snapshot: JobSnapshot) -> JobSnapshot | None:
        """The finished job, or ``None`` if it is still running at the deadline."""
        deadline = self._monotonic() + self._settings.wait_seconds
        delay = self._settings.poll_initial_seconds
        current = snapshot
        while current.state is not JobState.DONE:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return None
            await self._sleep(min(delay, remaining))
            delay = min(delay * 2, self._settings.poll_max_seconds)
            try:
                found = await self._warehouse.lookup(JobRef.of(job))
            except WarehouseError:
                continue
            if found is None:
                # Seen before but not visible now: keep the reference pending.
                continue
            current = found
        return current

    async def _stop_job(self, job: QueryJob | None) -> None:
        if job is None:
            return
        try:
            await self._warehouse.cancel(JobRef.of(job))
        except WarehouseError:
            # Best effort: its result is never released either way.
            return

    async def _move(
        self,
        op: ToolExecution,
        to: ToolExecutionStatus,
        attempt: int,
        *,
        error_code: ToolErrorCode | None = None,
        detail: str | None = None,
    ) -> ToolExecution:
        if op.status is to and to is not _S.RETRYING:
            return op
        return await self._operations.transition(
            op.operation_id, to, attempt=attempt, error_code=error_code, detail=detail
        )


def _statement(ref: JobRef, compiled: CompiledQuery, fingerprint: str) -> JobSubmission:
    return JobSubmission(
        ref=ref,
        sql=compiled.sql,
        parameters=compiled.parameters,
        maximum_bytes_billed=compiled.maximum_bytes_billed,
        fingerprint=fingerprint,
    )
