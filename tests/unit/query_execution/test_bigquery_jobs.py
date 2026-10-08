"""BigQuery job adapter against a stub client: request shape and error mapping.

The stub records what the adapter asks the SDK for and raises real
``google.api_core`` exceptions, so the job ID, byte cap, labels, parameter
types and sanitized errors are checked without network access.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest
from google.api_core import exceptions as api_exceptions
from google.cloud import bigquery

from retail_analytics.adapters.bigquery.jobs import (
    BigQueryQueryJobs,
    to_bigquery_parameter,
)
from retail_analytics.application.contracts.query_compiler import (
    ParameterType,
    QueryParameter,
)
from retail_analytics.application.contracts.warehouse_jobs import (
    JobRef,
    JobState,
    JobSubmission,
)
from retail_analytics.application.warehouse_jobs import (
    FINGERPRINT_LABEL,
    JobAlreadyExists,
    SubmissionRejected,
    WarehouseUnavailable,
)

REF = JobRef("proj", "US", "ra_abc_1")
SECRET = "0123456789abcdef-secret-pad"
SUBMISSION = JobSubmission(
    ref=REF,
    sql="SELECT 1 AS n WHERE @status = 'x'",
    parameters=(
        QueryParameter("status", ParameterType.STRING, "Complete"),
        QueryParameter(
            "_policy_product_ids",
            ParameterType.INT64,
            (1, 2, 3),
            array=True,
            trusted=True,
        ),
        QueryParameter(
            "_policy_ref_inner",
            ParameterType.STRING,
            SECRET,
            trusted=True,
            secret=True,
        ),
    ),
    maximum_bytes_billed=1024**3,
    fingerprint="f" * 32,
)


def query_job(
    state: str = "DONE",
    *,
    error: dict[str, str] | None = None,
    labels: dict[str, str] | None = None,
) -> bigquery.QueryJob:
    resource: dict[str, Any] = {
        "jobReference": {"projectId": "proj", "jobId": REF.job_id, "location": "US"},
        "configuration": {
            "query": {"query": "SELECT 1"},
            "labels": {FINGERPRINT_LABEL: "f" * 32} if labels is None else labels,
        },
        "status": {"state": state, **({"errorResult": error} if error else {})},
        "statistics": {
            "query": {
                "totalBytesProcessed": "100",
                "totalBytesBilled": "10485760",
                "cacheHit": False,
            }
        },
    }
    return bigquery.QueryJob.from_api_repr(
        resource, client=cast(bigquery.Client, SimpleNamespace(project="p"))
    )


def api_error(kind: type[Exception], *args: Any, **kwargs: Any) -> Exception:
    return kind(*args, **kwargs)


class StubClient:
    def __init__(self) -> None:
        self.queries: list[dict[str, Any]] = []
        self.query_error: Exception | None = None
        self.job: object = query_job()
        self.get_error: Exception | None = None
        self.cancel_error: Exception | None = None
        self.cancelled: list[str] = []

    def query(self, sql: str, **kwargs: Any) -> object:
        self.queries.append({"sql": sql, **kwargs})
        if self.query_error is not None:
            raise self.query_error
        if kwargs["job_config"].dry_run:
            return SimpleNamespace(total_bytes_processed=4096)
        return self.job

    def get_job(self, job_id: str, **kwargs: Any) -> object:
        if self.get_error is not None:
            raise self.get_error
        return self.job

    def cancel_job(self, job_id: str, **kwargs: Any) -> None:
        if self.cancel_error is not None:
            raise self.cancel_error
        self.cancelled.append(job_id)


def adapter(stub: StubClient) -> BigQueryQueryJobs:
    return BigQueryQueryJobs("proj", "US", cast(bigquery.Client, stub))


@pytest.mark.asyncio
async def test_submit_uses_recorded_id_byte_cap_labels_and_no_client_retry() -> None:
    stub = StubClient()
    snapshot = await adapter(stub).submit(SUBMISSION)

    (call,) = stub.queries
    config = call["job_config"]
    assert call["job_id"] == REF.job_id
    assert (call["project"], call["location"]) == ("proj", "US")
    assert call["job_retry"] is None
    assert call["api_method"] == "INSERT"
    assert config.maximum_bytes_billed == 1024**3
    assert config.labels[FINGERPRINT_LABEL] == "f" * 32
    assert not config.dry_run and not config.use_legacy_sql
    assert snapshot.state is JobState.DONE
    assert snapshot.fingerprint == "f" * 32
    assert snapshot.statistics.bytes_billed == 10485760


@pytest.mark.asyncio
async def test_dry_run_creates_no_job_and_returns_estimate() -> None:
    stub = StubClient()
    assert await adapter(stub).dry_run(SUBMISSION) == 4096
    (call,) = stub.queries
    assert call["job_config"].dry_run
    assert "job_id" not in call


def test_parameters_map_to_typed_scalars_and_arrays() -> None:
    scalar = to_bigquery_parameter(
        QueryParameter("d", ParameterType.DATE, date(2026, 9, 1))
    )
    assert isinstance(scalar, bigquery.ScalarQueryParameter)
    assert (scalar.type_, scalar.value) == ("DATE", date(2026, 9, 1))
    numeric = to_bigquery_parameter(
        QueryParameter("n", ParameterType.NUMERIC, Decimal("5.5"))
    )
    assert isinstance(numeric, bigquery.ScalarQueryParameter)
    assert numeric.to_api_repr()["parameterValue"]["value"] == "5.5"
    array = to_bigquery_parameter(SUBMISSION.parameters[1])
    assert isinstance(array, bigquery.ArrayQueryParameter)
    assert (array.array_type, array.values) == ("INT64", [1, 2, 3])
    secret = to_bigquery_parameter(SUBMISSION.parameters[2])
    assert isinstance(secret, bigquery.ScalarQueryParameter)
    assert (secret.type_, secret.value) == ("STRING", SECRET)
    with pytest.raises(ValueError, match="array flag"):
        to_bigquery_parameter(QueryParameter("a", ParameterType.INT64, (1,)))


@pytest.mark.asyncio
async def test_duplicate_id_is_reported_as_existing_job() -> None:
    stub = StubClient()
    stub.query_error = api_error(
        api_exceptions.Conflict, "Already Exists: Job proj:US.ra_abc_1"
    )
    with pytest.raises(JobAlreadyExists):
        await adapter(stub).submit(SUBMISSION)


@pytest.mark.parametrize(
    ("error", "expected", "reason"),
    [
        (
            api_error(
                api_exceptions.BadRequest,
                "Syntax error near 'Complete'",
                errors=[{"reason": "invalidQuery", "message": "near 'Complete'"}],
            ),
            SubmissionRejected,
            "invalidQuery",
        ),
        (
            api_error(
                api_exceptions.Forbidden,
                "Quota exceeded",
                errors=[{"reason": "quotaExceeded"}],
            ),
            SubmissionRejected,
            "quotaExceeded",
        ),
        (
            api_error(
                api_exceptions.Forbidden,
                "who knows",
                errors=[{"reason": "someNewReason: 'Complete'"}],
            ),
            SubmissionRejected,
            "other",
        ),
        (
            api_error(api_exceptions.InternalServerError, "boom"),
            WarehouseUnavailable,
            "unavailable",
        ),
        (
            api_error(api_exceptions.DeadlineExceeded, "slow"),
            WarehouseUnavailable,
            "timeout_unconfirmed",
        ),
        (ConnectionError("reset"), WarehouseUnavailable, "unavailable"),
    ],
)
@pytest.mark.asyncio
async def test_submission_errors_are_sanitized(
    error: Exception, expected: type[Exception], reason: str
) -> None:
    stub = StubClient()
    stub.query_error = error
    with pytest.raises(expected) as raised:
        await adapter(stub).submit(SUBMISSION)
    assert getattr(raised.value, "reason", None) == reason
    assert "Complete" not in str(raised.value)
    assert raised.value.__cause__ is None


@pytest.mark.asyncio
async def test_lookup_maps_missing_running_and_failed_jobs() -> None:
    stub = StubClient()
    stub.get_error = api_error(api_exceptions.NotFound, "Not found: Job")
    assert await adapter(stub).lookup(REF) is None

    stub.get_error = None
    stub.job = query_job("RUNNING")
    running = await adapter(stub).lookup(REF)
    assert running is not None and running.state is JobState.RUNNING

    stub.job = query_job(error={"reason": "quotaExceeded", "message": "value 42"})
    failed = await adapter(stub).lookup(REF)
    assert failed is not None and failed.error_reason == "quotaExceeded"

    stub.job = query_job(error={"reason": "weird", "message": "value 42"})
    odd = await adapter(stub).lookup(REF)
    assert odd is not None and odd.error_reason == "other"

    stub.get_error = api_error(api_exceptions.ServiceUnavailable, "down")
    with pytest.raises(WarehouseUnavailable):
        await adapter(stub).lookup(REF)


@pytest.mark.asyncio
async def test_job_of_another_kind_is_never_taken_for_ours() -> None:
    stub = StubClient()
    stub.job = SimpleNamespace(state="DONE")
    snapshot = await adapter(stub).lookup(REF)
    assert snapshot is not None and snapshot.fingerprint is None


@pytest.mark.asyncio
async def test_cancel_tolerates_missing_jobs() -> None:
    stub = StubClient()
    await adapter(stub).cancel(REF)
    assert stub.cancelled == [REF.job_id]
    stub.cancel_error = api_error(api_exceptions.NotFound, "gone")
    await adapter(stub).cancel(REF)
    stub.cancel_error = api_error(api_exceptions.ServiceUnavailable, "down")
    with pytest.raises(WarehouseUnavailable):
        await adapter(stub).cancel(REF)


@pytest.mark.asyncio
async def test_fetch_rows_reports_completeness_honestly() -> None:
    schema = [bigquery.SchemaField("n", "INT64")]
    rows = [bigquery.Row((i,), {"n": 0}) for i in range(3)]

    class Rows(list[bigquery.Row]):
        def __init__(self, total: int) -> None:
            super().__init__(rows)
            self.schema = schema
            self.total_rows = total

    def job(total: int) -> bigquery.QueryJob:
        done = query_job()
        setattr(done, "result", lambda **kwargs: Rows(total))  # noqa: B010
        return done

    stub = StubClient()
    stub.job = job(3)
    complete = await adapter(stub).fetch_rows(REF, max_rows=10)
    assert complete.columns == ("n",)
    assert list(complete.rows) == [(0,), (1,), (2,)]
    assert complete.complete

    stub.job = job(900)
    partial = await adapter(stub).fetch_rows(REF, max_rows=3)
    assert not partial.complete
