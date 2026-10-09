"""Live durable execution against BigQuery: tiny bounded queries on public tables.

Runs the real adapter and service (in-memory records, test authority) to
check that recorded job IDs, byte caps and labels reach BigQuery, that a lost
submission response reconciles the existing job, and that resubmitting a
recorded job ID is refused as a duplicate instead of running twice. Each
query reads a few hundred KiB (10 MiB minimum billing). Skipped without a
configured project and application default credentials.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from google.cloud import bigquery

from retail_analytics.adapters.bigquery.jobs import BigQueryQueryJobs
from retail_analytics.adapters.google_access import create_bigquery_client
from retail_analytics.adapters.sql_compiler import ScopedSqlglotCompilers
from retail_analytics.application.access_check import PUBLIC_DATASET
from retail_analytics.application.contracts.authorization import Principal
from retail_analytics.application.contracts.query_compiler import AnalysisQuery
from retail_analytics.application.contracts.result_privacy import QueryRows
from retail_analytics.application.contracts.warehouse_jobs import (
    JobRef,
    JobSnapshot,
    JobSubmission,
)
from retail_analytics.application.query_execution import (
    QueryAttempt,
    QueryExecutionService,
    QueryExecutionSettings,
    QueryOutcomeUnknown,
    QuerySucceeded,
)
from retail_analytics.application.result_privacy import ResultPrivacyBoundary
from retail_analytics.application.warehouse_jobs import (
    FINGERPRINT_LABEL,
    JobAlreadyExists,
    WarehouseUnavailable,
)
from retail_analytics.bootstrap.config import load_backend_settings
from retail_analytics.domain.access import ProductScope
from tests.unit.query_execution.fakes import (
    FakeAuthority,
    MemoryOperations,
    MemoryQueryJobs,
)

pytestmark = pytest.mark.live
ROOT = Path(__file__).resolve().parents[2]
EXECUTIVE = "live-test"
SCOPE = ProductScope(frozenset(str(p) for p in range(1, 2001)), 1)
QUERY = AnalysisQuery(
    "SELECT category, COUNT(*) AS n FROM products "
    "WHERE catalog_price >= @floor GROUP BY category ORDER BY n DESC, category LIMIT 5",
    {"floor": 1},
)
MAX_BILLED = 50 * 1024 * 1024


@pytest.fixture(scope="module")
def project() -> tuple[str, str, bigquery.Client]:
    settings = load_backend_settings(environ={}, env_file=ROOT / ".env")
    if settings.bigquery_project is None:
        pytest.skip("BIGQUERY_PROJECT not set")
    try:
        client = create_bigquery_client(
            settings.bigquery_project, settings.bigquery_location
        )
    except Exception:  # any credential failure means "not configured"
        pytest.skip("application default credentials not configured")
    return settings.bigquery_project, settings.bigquery_location, client


class LoseFirstResponse:
    """Submits for real, then reports the response as lost once."""

    def __init__(self, inner: BigQueryQueryJobs) -> None:
        self.inner = inner
        self.lost = False

    async def dry_run(self, submission: JobSubmission) -> int:
        return await self.inner.dry_run(submission)

    async def submit(self, submission: JobSubmission) -> JobSnapshot:
        snapshot = await self.inner.submit(submission)
        if not self.lost:
            self.lost = True
            raise WarehouseUnavailable("timeout_unconfirmed")
        return snapshot

    async def lookup(self, ref: JobRef) -> JobSnapshot | None:
        return await self.inner.lookup(ref)

    async def fetch_rows(self, ref: JobRef, *, max_rows: int) -> QueryRows:
        return await self.inner.fetch_rows(ref, max_rows=max_rows)

    async def cancel(self, ref: JobRef) -> None:
        await self.inner.cancel(ref)


def service(
    project: tuple[str, str, bigquery.Client],
    warehouse: BigQueryQueryJobs | LoseFirstResponse,
    operations: MemoryOperations,
) -> QueryExecutionService:
    name, location, _ = project
    return QueryExecutionService(
        settings=QueryExecutionSettings(
            project=name, location=location, job_namespace="ratest", wait_seconds=90
        ),
        authority=FakeAuthority(EXECUTIVE, SCOPE),
        compilers=ScopedSqlglotCompilers(PUBLIC_DATASET, None),
        boundary=ResultPrivacyBoundary(),
        warehouse=warehouse,
        operations=operations,
        jobs=MemoryQueryJobs(operations),
    )


def attempt(op: str, n: int) -> QueryAttempt:
    return QueryAttempt(
        Principal(EXECUTIVE, frozenset({"analysis:read"})), "run-live", op, n, QUERY
    )


@pytest.mark.asyncio
async def test_live_query_records_job_and_returns_sanitized_rows(
    project: tuple[str, str, bigquery.Client],
) -> None:
    name, location, client = project
    jobs = BigQueryQueryJobs(name, location, client)
    operations = MemoryOperations()
    svc = service(project, jobs, operations)
    op = f"live-{uuid.uuid4().hex}"

    outcome = await svc.execute(attempt(op, 1))

    assert isinstance(outcome, QuerySucceeded), outcome
    assert [c.name for c in outcome.result.columns] == ["category", "n"]
    assert 0 < len(outcome.result.rows) <= 5
    assert not outcome.result.truncated
    job = client.get_job(outcome.job.job_id, project=name, location=location)
    assert isinstance(job, bigquery.QueryJob)
    assert job.labels[FINGERPRINT_LABEL] == outcome.job.query_fingerprint
    assert job.maximum_bytes_billed == outcome.compiled.maximum_bytes_billed
    assert (job.total_bytes_billed or 0) <= MAX_BILLED
    assert operations.statuses(op)[-1].value == "succeeded"


@pytest.mark.asyncio
async def test_live_lost_response_reconciles_and_duplicate_is_refused(
    project: tuple[str, str, bigquery.Client],
) -> None:
    name, location, client = project
    jobs = BigQueryQueryJobs(name, location, client)
    flaky = LoseFirstResponse(jobs)
    operations = MemoryOperations()
    svc = service(project, flaky, operations)
    op = f"live-{uuid.uuid4().hex}"

    unknown = await svc.execute(attempt(op, 1))
    assert isinstance(unknown, QueryOutcomeUnknown) and unknown.job is not None

    outcome = await svc.execute(attempt(op, 2))
    assert isinstance(outcome, QuerySucceeded), outcome
    assert outcome.job.job_id == unknown.job.job_id
    assert flaky.lost

    # The warehouse itself refuses a second job with the recorded ID.
    submitted = client.get_job(unknown.job.job_id, project=name, location=location)
    assert isinstance(submitted, bigquery.QueryJob)
    with pytest.raises(JobAlreadyExists):
        await jobs.submit(
            JobSubmission(
                JobRef(name, location, unknown.job.job_id),
                outcome.compiled.sql,
                outcome.compiled.parameters,
                outcome.compiled.maximum_bytes_billed,
                outcome.job.query_fingerprint,
            )
        )


@pytest.mark.asyncio
async def test_live_unknown_job_lookup_and_cancel_are_harmless(
    project: tuple[str, str, bigquery.Client],
) -> None:
    name, location, client = project
    jobs = BigQueryQueryJobs(name, location, client)
    ref = JobRef(name, location, f"ratest_missing_{uuid.uuid4().hex}")
    assert await jobs.lookup(ref) is None
    await jobs.cancel(ref)
