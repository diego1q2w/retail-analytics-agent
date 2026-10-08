"""BigQuery implementation of ``WarehouseQueryJobs``.

Jobs are created with ``jobs.insert`` and exactly the job ID the service
recorded; the client's own job retry is disabled so it never resubmits under
a new ID. A duplicate ID surfaces as ``JobAlreadyExists`` and the service
reconciles it. Every job carries ``maximum_bytes_billed`` and the statement
fingerprint label.

The source tables are public: these credentials can read them directly, so
the application query path (compiler, scope binding, result boundary) is the
enforcement boundary, not IAM on the rows. The adapter only issues query jobs
without destination tables.

SDK exceptions become port errors with a sanitized reason only; provider
messages (which can quote SQL fragments or values) are never kept.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping

from google.api_core import exceptions as api_exceptions
from google.auth import exceptions as auth_exceptions
from google.cloud import bigquery

from retail_analytics.adapters.google_access import create_bigquery_client
from retail_analytics.application.contracts.query_compiler import QueryParameter
from retail_analytics.application.contracts.result_privacy import QueryRows
from retail_analytics.application.contracts.warehouse_jobs import (
    JobRef,
    JobSnapshot,
    JobState,
    JobStatistics,
    JobSubmission,
)
from retail_analytics.application.warehouse_jobs import (
    FINGERPRINT_LABEL,
    JobAlreadyExists,
    SubmissionRejected,
    WarehouseUnavailable,
    sanitize_reason,
)

APP_LABEL = ("app", "retail-analytics")
DEFAULT_REQUEST_TIMEOUT = 30.0

type BigQueryParameter = bigquery.ScalarQueryParameter | bigquery.ArrayQueryParameter

_STATES = {
    "PENDING": JobState.PENDING,
    "RUNNING": JobState.RUNNING,
    "DONE": JobState.DONE,
}
# Errors after which a request is known not to have created anything.
_REJECTED = (
    api_exceptions.BadRequest,
    api_exceptions.Forbidden,
    api_exceptions.TooManyRequests,
    api_exceptions.NotFound,
    api_exceptions.Unauthorized,
)


def to_bigquery_parameter(parameter: QueryParameter) -> BigQueryParameter:
    """Typed parameter for a job; arrays must be flagged and be tuples."""
    kind = parameter.type.value
    if parameter.array != isinstance(parameter.value, tuple):
        raise ValueError(f"parameter {parameter.name!r}: array flag does not match")
    if isinstance(parameter.value, tuple):
        return bigquery.ArrayQueryParameter(parameter.name, kind, list(parameter.value))
    return bigquery.ScalarQueryParameter(parameter.name, kind, parameter.value)


def _reason(exc: BaseException) -> str:
    errors = getattr(exc, "errors", None)
    if isinstance(errors, list) and errors and isinstance(errors[0], Mapping):
        value = errors[0].get("reason")
        if isinstance(value, str):
            return value
    return "other"


def _snapshot(ref: JobRef, job: object) -> JobSnapshot:
    if not isinstance(job, bigquery.QueryJob):
        # Some other kind of job holds the ID: never treat it as ours.
        return JobSnapshot(ref, JobState.DONE, None, "other")
    labels = job.labels or {}
    error = job.error_result or None
    reason = None
    if error is not None:
        raw = error.get("reason") if isinstance(error, Mapping) else None
        reason = sanitize_reason(raw if isinstance(raw, str) else None)
    return JobSnapshot(
        ref=ref,
        state=_STATES.get(str(job.state), JobState.PENDING),
        fingerprint=labels.get(FINGERPRINT_LABEL),
        error_reason=reason,
        statistics=JobStatistics(
            bytes_processed=job.total_bytes_processed,
            bytes_billed=job.total_bytes_billed,
            cache_hit=job.cache_hit,
        ),
    )


def _unavailable(exc: BaseException) -> WarehouseUnavailable:
    if isinstance(exc, api_exceptions.DeadlineExceeded | TimeoutError):
        return WarehouseUnavailable("timeout_unconfirmed")
    return WarehouseUnavailable("unavailable")


class BigQueryQueryJobs:
    def __init__(
        self,
        project: str,
        location: str,
        client: bigquery.Client | None = None,
        *,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
        client_factory: Callable[[str, str], bigquery.Client] = create_bigquery_client,
    ) -> None:
        self._project = project
        self._location = location
        self._client = client
        self._timeout = request_timeout
        self._factory = client_factory

    def _get_client(self) -> bigquery.Client:
        if self._client is None:
            self._client = self._factory(self._project, self._location)
        return self._client

    async def dry_run(self, submission: JobSubmission) -> int:
        return await asyncio.to_thread(self._dry_run, submission)

    async def submit(self, submission: JobSubmission) -> JobSnapshot:
        return await asyncio.to_thread(self._submit, submission)

    async def lookup(self, ref: JobRef) -> JobSnapshot | None:
        return await asyncio.to_thread(self._lookup, ref)

    async def fetch_rows(self, ref: JobRef, *, max_rows: int) -> QueryRows:
        return await asyncio.to_thread(self._fetch_rows, ref, max_rows)

    async def cancel(self, ref: JobRef) -> None:
        await asyncio.to_thread(self._cancel, ref)

    # -- blocking SDK calls ----------------------------------------------

    def _config(
        self, submission: JobSubmission, *, dry_run: bool
    ) -> bigquery.QueryJobConfig:
        config = bigquery.QueryJobConfig(
            dry_run=dry_run,
            use_legacy_sql=False,
            use_query_cache=not dry_run,
            maximum_bytes_billed=submission.maximum_bytes_billed,
            query_parameters=[to_bigquery_parameter(p) for p in submission.parameters],
        )
        if not dry_run and submission.timeout_seconds is not None:
            config.job_timeout_ms = int(submission.timeout_seconds * 1000)
        if not dry_run:
            config.labels = {
                APP_LABEL[0]: APP_LABEL[1],
                FINGERPRINT_LABEL: submission.fingerprint,
            }
        return config

    def _dry_run(self, submission: JobSubmission) -> int:
        try:
            job = self._get_client().query(
                submission.sql,
                job_config=self._config(submission, dry_run=True),
                project=submission.ref.project,
                location=submission.ref.location,
                timeout=self._timeout,
                job_retry=None,
            )
        except _REJECTED as exc:
            raise SubmissionRejected(_reason(exc)) from None
        except auth_exceptions.GoogleAuthError:
            raise SubmissionRejected("credentials") from None
        except Exception as exc:
            raise _unavailable(exc) from None
        return int(job.total_bytes_processed or 0)

    def _submit(self, submission: JobSubmission) -> JobSnapshot:
        ref = submission.ref
        try:
            job = self._get_client().query(
                submission.sql,
                job_config=self._config(submission, dry_run=False),
                job_id=ref.job_id,
                project=ref.project,
                location=ref.location,
                timeout=self._timeout,
                job_retry=None,
                api_method="INSERT",
            )
        except api_exceptions.Conflict:
            raise JobAlreadyExists("duplicate") from None
        except _REJECTED as exc:
            raise SubmissionRejected(_reason(exc)) from None
        except auth_exceptions.GoogleAuthError:
            raise SubmissionRejected("credentials") from None
        except Exception as exc:
            raise _unavailable(exc) from None
        return _snapshot(ref, job)

    def _lookup(self, ref: JobRef) -> JobSnapshot | None:
        try:
            job = self._get_client().get_job(
                ref.job_id,
                project=ref.project,
                location=ref.location,
                timeout=self._timeout,
            )
        except api_exceptions.NotFound:
            return None
        except Exception as exc:
            raise _unavailable(exc) from None
        return _snapshot(ref, job)

    def _fetch_rows(self, ref: JobRef, max_rows: int) -> QueryRows:
        try:
            job = self._get_client().get_job(
                ref.job_id,
                project=ref.project,
                location=ref.location,
                timeout=self._timeout,
            )
            if not isinstance(job, bigquery.QueryJob):
                raise SubmissionRejected("other")
            rows = job.result(
                max_results=max_rows, timeout=self._timeout, job_retry=None
            )
            columns = tuple(field.name for field in rows.schema)
            values = [tuple(row.values()) for row in rows]
            total = rows.total_rows
        except SubmissionRejected:
            raise
        except api_exceptions.NotFound:
            # Results of finished jobs expire; nothing can be read any more.
            raise SubmissionRejected("notFound") from None
        except Exception as exc:
            raise _unavailable(exc) from None
        complete = len(values) < max_rows if total is None else total <= len(values)
        return QueryRows(columns, values, complete=complete)

    def _cancel(self, ref: JobRef) -> None:
        try:
            self._get_client().cancel_job(
                ref.job_id,
                project=ref.project,
                location=ref.location,
                timeout=self._timeout,
            )
        except api_exceptions.NotFound:
            return
        except Exception as exc:
            raise _unavailable(exc) from None
