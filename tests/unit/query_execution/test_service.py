"""Durable query execution: submission, reconciliation and release under faults.

Statements run on the DuckDB oracle with the production keyed derivations, so
released results are the real sanitized shape. Every fault leaves the same
records a crashed or retried worker would find; a "restart" is a new service
instance over the same records and warehouse.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest
from duckdb import DuckDBPyConnection as Connection

from retail_analytics.application.contracts.authorization import Principal
from retail_analytics.application.contracts.persistence import OperationRequest
from retail_analytics.application.contracts.query_compiler import (
    AnalysisQuery,
    CompiledQuery,
)
from retail_analytics.application.contracts.tools import ExecutionContext
from retail_analytics.application.contracts.warehouse_jobs import (
    JobRef,
    JobSnapshot,
    JobSubmission,
)
from retail_analytics.application.ports.query_execution import (
    QueryAdmission,
    QueryUsageRecorder,
)
from retail_analytics.application.query_execution import (
    QUERY_CAPABILITY,
    QueryAttempt,
    QueryCancelled,
    QueryExecutionService,
    QueryExecutionSettings,
    QueryFailed,
    QueryNotAdmitted,
    QueryOutcome,
    QueryOutcomeUnknown,
    QueryPending,
    QuerySucceeded,
    is_compiler_rejection,
    query_fingerprint,
)
from retail_analytics.application.result_privacy import (
    ResultLimits,
    ResultPrivacyBoundary,
    TruncationReason,
)
from retail_analytics.domain.executions import ToolExecutionStatus, query_job_id
from retail_analytics.domain.operations import SideEffect, ToolErrorCode
from tests.unit.privacy.support import (
    COMPILERS,
    EXEC_A,
    MASTER_KEY,
    SCOPE_A,
    assert_no_pii,
    customer_database,
    released,
)
from tests.unit.query_execution.fakes import (
    T0,
    CrashAfterSubmit,
    FakeAuthority,
    FakeWarehouse,
    MemoryOperations,
    MemoryQueryJobs,
    oracle_runner,
)

S = ToolExecutionStatus
RUN = "run-1"
OP = "op-0001"
PRINCIPAL = Principal(EXEC_A, frozenset({"analysis:read"}))
TOP_CUSTOMERS = (
    "SELECT s.customer_ref AS customer, SUM(s.sale_amount) AS completed_sales "
    "FROM sales_items s JOIN customers c ON s.customer_ref = c.customer_ref "
    "WHERE s.item_status = @status "
    "GROUP BY customer "
    "ORDER BY completed_sales DESC LIMIT 10"
)
VALUES = {"status": "Complete"}
SETTINGS = QueryExecutionSettings(
    project="test-project", location="US", wait_seconds=10.0
)


@dataclass
class FakeClock:
    now: float = 0.0

    async def sleep(self, seconds: float) -> None:
        self.now += seconds

    def monotonic(self) -> float:
        return self.now

    def wall(self) -> datetime:
        return T0 + timedelta(seconds=self.now)


@dataclass
class RecordingAdmission:
    refuse: bool = False
    calls: list[tuple[str, int, int]] | None = None

    async def admit(
        self,
        context: ExecutionContext,
        operation_id: str,
        submission: int,
        compiled: CompiledQuery,
        estimated_bytes: int,
    ) -> None:
        if self.calls is None:
            self.calls = []
        self.calls.append((operation_id, submission, estimated_bytes))
        if self.refuse:
            raise QueryNotAdmitted(
                ToolErrorCode.BUDGET_EXCEEDED, "run_bytes", "The run's budget is spent."
            )


class Harness:
    def __init__(self, db: Connection) -> None:
        self.db = db
        self.operations = MemoryOperations()
        self.jobs = MemoryQueryJobs(self.operations)
        self.warehouse = FakeWarehouse(oracle_runner(db))
        self.authority = FakeAuthority(EXEC_A, SCOPE_A)
        self.clock = FakeClock()
        self.settings = SETTINGS
        self.admission: QueryAdmission | None = None
        self.usage: QueryUsageRecorder | None = None
        self.limits: ResultLimits | None = None

    def service(self) -> QueryExecutionService:
        """A fresh instance: what a restarted worker would build."""
        return QueryExecutionService(
            settings=self.settings,
            authority=self.authority,
            compilers=COMPILERS,
            boundary=ResultPrivacyBoundary(self.limits),
            warehouse=self.warehouse,
            operations=self.operations,
            jobs=self.jobs,
            admission=self.admission,
            usage=self.usage,
            sleep=self.clock.sleep,
            monotonic=self.clock.monotonic,
            clock=self.clock.wall,
        )

    async def run(
        self, attempt: int = 1, query: str = TOP_CUSTOMERS, op: str = OP
    ) -> QueryOutcome:
        return await self.service().execute(
            QueryAttempt(PRINCIPAL, RUN, op, attempt, AnalysisQuery(query, VALUES))
        )

    def statuses(self, op: str = OP) -> list[ToolExecutionStatus]:
        return self.operations.statuses(op)

    def status(self, op: str = OP) -> ToolExecutionStatus:
        return self.operations.records[op].status


@pytest.fixture
def db() -> Iterator[Connection]:
    connection = customer_database()
    yield connection
    connection.close()


@pytest.fixture
def h(db: Connection) -> Harness:
    return Harness(db)


def expected_rows(db: Connection) -> tuple[tuple[object, ...], ...]:
    return released(db, EXEC_A, TOP_CUSTOMERS, SCOPE_A, VALUES).rows


def succeeded(outcome: QueryOutcome) -> QuerySucceeded:
    assert isinstance(outcome, QuerySucceeded), outcome
    return outcome


def failed(outcome: QueryOutcome) -> QueryFailed:
    assert isinstance(outcome, QueryFailed), outcome
    return outcome


# --- success path ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_records_one_reference_before_submission(
    h: Harness, db: Connection
) -> None:
    seen_at_submit: list[int] = []

    def check(submission: JobSubmission) -> None:
        recorded = h.jobs.rows.get(OP, [])
        assert [j.job_id for j in recorded] == [submission.ref.job_id]
        assert h.status() is S.SUBMITTING
        seen_at_submit.append(len(recorded))

    h.warehouse.before_submit = check
    outcome = succeeded(await h.run())

    assert seen_at_submit == [1]
    (job,) = h.jobs.rows[OP]
    assert job.job_id == query_job_id("ra", OP, 1)
    assert (job.project, job.location, job.submission) == ("test-project", "US", 1)
    assert job.authorization_version == SCOPE_A.entitlement_version
    assert job.query_fingerprint == query_fingerprint(outcome.compiled)
    assert h.warehouse.created == [job.job_id]
    assert outcome.job == job
    assert outcome.result.rows == expected_rows(db)
    assert_no_pii(outcome.result, raw_ids=(10, 30, 40, 50))
    assert outcome.statistics.bytes_billed is not None
    assert h.statuses() == [S.PREPARED, S.SUBMITTING, S.RUNNING, S.SUCCEEDED]


@pytest.mark.asyncio
async def test_job_carries_byte_cap_parameters_and_fingerprint(h: Harness) -> None:
    outcome = succeeded(await h.run())
    submission = h.warehouse.jobs[outcome.job.job_id].submission
    compiled = outcome.compiled

    assert submission.maximum_bytes_billed == compiled.maximum_bytes_billed
    assert submission.parameters == compiled.parameters
    assert submission.fingerprint == outcome.job.query_fingerprint
    assert any(p.secret for p in submission.parameters)
    # Dry run first, with the identical statement.
    assert h.warehouse.dry_runs[0].sql == submission.sql


@pytest.mark.asyncio
async def test_secrets_and_sql_stay_out_of_records_and_reprs(h: Harness) -> None:
    outcome = succeeded(await h.run())
    submission = h.warehouse.jobs[outcome.job.job_id].submission
    secrets = [str(p.value) for p in submission.parameters if p.secret]
    assert secrets

    texts = [
        repr(submission),
        repr(outcome),
        repr(h.jobs.rows[OP]),
        repr(h.operations.events[OP]),
        repr(h.operations.records[OP]),
    ]
    for text in texts:
        assert "SELECT" not in text
        assert "Complete" not in text
        assert MASTER_KEY.decode() not in text
        for secret in secrets:
            assert secret not in text


@pytest.mark.asyncio
async def test_retried_attempt_after_success_releases_again_without_new_job(
    h: Harness, db: Connection
) -> None:
    first = succeeded(await h.run(attempt=1))
    again = succeeded(await h.run(attempt=2))

    assert again.result.rows == first.result.rows == expected_rows(db)
    assert len(h.warehouse.created) == 1
    assert h.status() is S.SUCCEEDED


@pytest.mark.asyncio
async def test_result_limits_flag_truncation(h: Harness) -> None:
    h.limits = ResultLimits(max_rows=2)
    h.settings = QueryExecutionSettings(
        project="test-project", location="US", max_rows=2
    )

    outcome = succeeded(await h.run())

    assert len(outcome.result.rows) == 2
    assert outcome.result.truncation is TruncationReason.ROWS


# --- lost responses, crashes and restarts --------------------------------------


@pytest.mark.asyncio
async def test_lost_submission_response_reconciles_the_existing_job(
    h: Harness, db: Connection
) -> None:
    h.warehouse.submit_faults = ["lost_response"]

    unknown = await h.run(attempt=1)
    assert isinstance(unknown, QueryOutcomeUnknown)
    assert unknown.job is not None
    assert h.status() is S.OUTCOME_UNKNOWN

    outcome = succeeded(await h.run(attempt=2))

    assert h.warehouse.created == [unknown.job.job_id]
    assert h.warehouse.submit_calls == 1
    assert outcome.result.rows == expected_rows(db)
    assert h.statuses() == [
        S.PREPARED,
        S.SUBMITTING,
        S.OUTCOME_UNKNOWN,
        S.RUNNING,
        S.SUCCEEDED,
    ]


@pytest.mark.asyncio
async def test_process_crash_after_submit_then_restart_never_duplicates(
    h: Harness, db: Connection
) -> None:
    h.warehouse.submit_faults = ["crash"]
    with pytest.raises(CrashAfterSubmit):
        await h.run(attempt=1)
    assert h.status() is S.SUBMITTING

    outcome = succeeded(await h.run(attempt=2))  # new service instance

    assert len(h.warehouse.created) == 1
    assert h.warehouse.submit_calls == 1
    assert len(h.jobs.rows[OP]) == 1
    assert outcome.result.rows == expected_rows(db)


@pytest.mark.asyncio
async def test_crash_before_the_job_existed_resubmits_the_same_job_id(
    h: Harness,
) -> None:
    h.warehouse.submit_faults = ["unavailable"]

    unknown = await h.run(attempt=1)
    assert isinstance(unknown, QueryOutcomeUnknown)
    assert h.warehouse.created == []

    succeeded(await h.run(attempt=2))

    (job,) = h.jobs.rows[OP]
    assert h.warehouse.created == [job.job_id]
    assert h.warehouse.submit_calls == 2


@pytest.mark.asyncio
async def test_duplicate_submission_is_followed_not_rerun(h: Harness) -> None:
    # A lost response whose job also could not be looked up at first.
    h.warehouse.submit_faults = ["lost_response"]
    h.warehouse.lookup_faults = ["unavailable"]
    assert isinstance(await h.run(attempt=1), QueryOutcomeUnknown)
    assert isinstance(await h.run(attempt=2), QueryOutcomeUnknown)

    succeeded(await h.run(attempt=3))

    assert len(h.warehouse.created) == 1


@pytest.mark.asyncio
async def test_resubmitting_an_existing_id_reconciles_the_duplicate(
    h: Harness,
) -> None:
    # The job exists, but a stale lookup said it did not: the warehouse
    # rejects the resubmission as a duplicate and the job is followed.
    h.warehouse.submit_faults = ["lost_response"]
    assert isinstance(await h.run(attempt=1), QueryOutcomeUnknown)
    real_lookup = h.warehouse.lookup
    calls = 0

    async def stale_once(ref: JobRef) -> JobSnapshot | None:
        nonlocal calls
        calls += 1
        return None if calls == 1 else await real_lookup(ref)

    h.warehouse.lookup = stale_once  # type: ignore[method-assign]
    succeeded(await h.run(attempt=2))

    assert len(h.warehouse.created) == 1
    assert h.warehouse.submit_calls == 2


# --- slow jobs, timeouts and cancellation --------------------------------------


@pytest.mark.asyncio
async def test_slow_job_returns_pending_and_a_later_attempt_follows_it(
    h: Harness, db: Connection
) -> None:
    h.warehouse.polls_to_finish = 1000

    pending = await h.run(attempt=1)
    assert isinstance(pending, QueryPending)
    assert h.status() is S.RUNNING
    assert h.clock.now >= SETTINGS.wait_seconds

    h.warehouse.finish(pending.job.job_id)
    outcome = succeeded(await h.run(attempt=2))

    assert len(h.warehouse.created) == 1
    assert outcome.result.rows == expected_rows(db)


@pytest.mark.asyncio
async def test_cancel_stops_the_job_and_releases_nothing(h: Harness) -> None:
    h.warehouse.polls_to_finish = 1000
    pending = await h.run(attempt=1)
    assert isinstance(pending, QueryPending)

    cancelled = await h.service().cancel(RUN, OP)

    assert cancelled == QueryCancelled(pending.job, confirmed=True)
    assert h.warehouse.cancels == [pending.job.job_id]
    assert h.status() is S.CANCELLED
    after = await h.run(attempt=2)
    assert isinstance(after, QueryCancelled)
    assert len(h.warehouse.created) == 1


@pytest.mark.asyncio
async def test_cancel_before_submission_needs_no_warehouse_call(h: Harness) -> None:
    await h.operations.begin(
        OperationRequest(OP, RUN, QUERY_CAPABILITY, 1, SideEffect.EXTERNAL_JOB)
    )
    assert await h.service().cancel(RUN, OP) == QueryCancelled(None, confirmed=True)
    assert h.warehouse.cancels == []
    assert h.status() is S.CANCELLED


@pytest.mark.asyncio
async def test_cancel_of_another_runs_operation_is_refused(h: Harness) -> None:
    succeeded(await h.run())
    outcome = failed(await h.service().cancel("other-run", OP))
    assert outcome.code is ToolErrorCode.ACCESS_DENIED
    assert h.status() is S.SUCCEEDED


# --- warehouse failures -------------------------------------------------------


@pytest.mark.asyncio
async def test_quota_exceeded_job_fails_without_retry(h: Harness) -> None:
    h.warehouse.job_errors = ["quotaExceeded"]

    outcome = failed(await h.run())

    assert outcome.code is ToolErrorCode.BUDGET_EXCEEDED
    assert not outcome.retryable
    assert h.status() is S.FAILED
    assert h.operations.records[OP].error_detail == "job_quotaExceeded"
    # A retried activity reports the recorded failure; nothing is resubmitted.
    again = failed(await h.run(attempt=2))
    assert again.code is ToolErrorCode.BUDGET_EXCEEDED
    assert len(h.warehouse.created) == 1


@pytest.mark.asyncio
async def test_quota_exceeded_at_submission_fails(h: Harness) -> None:
    h.warehouse.submit_faults = ["rejected:quotaExceeded"]
    outcome = failed(await h.run())
    assert outcome.code is ToolErrorCode.BUDGET_EXCEEDED
    assert h.warehouse.created == []


@pytest.mark.asyncio
async def test_rate_limited_submission_retries_with_the_same_job_id(
    h: Harness,
) -> None:
    h.warehouse.submit_faults = ["rejected:rateLimitExceeded"]

    outcome = failed(await h.run(attempt=1))
    assert outcome.retryable and outcome.code is ToolErrorCode.TEMPORARY_FAILURE
    assert h.status() is S.RETRYING

    succeeded(await h.run(attempt=2))
    assert [j.submission for j in h.jobs.rows[OP]] == [1]
    assert h.warehouse.created == [query_job_id("ra", OP, 1)]


@pytest.mark.asyncio
async def test_transient_job_failure_makes_a_second_submission(h: Harness) -> None:
    h.warehouse.job_errors = ["backendError"]

    outcome = failed(await h.run(attempt=1))
    assert outcome.retryable
    assert h.status() is S.RETRYING

    succeeded(await h.run(attempt=2))
    jobs = h.jobs.rows[OP]
    assert [j.submission for j in jobs] == [1, 2]
    assert h.warehouse.created == [query_job_id("ra", OP, n) for n in (1, 2)]


@pytest.mark.asyncio
async def test_crash_after_transient_job_failure_still_resubmits_once(
    h: Harness,
) -> None:
    # The worker died before recording the failed job; the next attempt
    # discovers it by lookup.
    h.warehouse.job_errors = ["backendError"]
    h.warehouse.submit_faults = ["crash"]
    with pytest.raises(CrashAfterSubmit):
        await h.run(attempt=1)

    succeeded(await h.run(attempt=2))
    assert len(h.warehouse.created) == 2
    assert succeeded(await h.run(attempt=3)) is not None
    assert len(h.warehouse.created) == 2


@pytest.mark.asyncio
async def test_job_not_found_during_reconciliation_resubmits_same_reference(
    h: Harness,
) -> None:
    h.warehouse.submit_faults = ["lost_response"]
    unknown = await h.run(attempt=1)
    assert isinstance(unknown, QueryOutcomeUnknown) and unknown.job is not None
    # The warehouse lost the job entirely (e.g. never persisted).
    del h.warehouse.jobs[unknown.job.job_id]

    succeeded(await h.run(attempt=2))
    assert h.warehouse.created == [unknown.job.job_id, unknown.job.job_id]
    assert len(h.jobs.rows[OP]) == 1


@pytest.mark.asyncio
async def test_lookup_failure_is_an_unknown_outcome_not_a_failure(h: Harness) -> None:
    h.warehouse.polls_to_finish = 1000
    assert isinstance(await h.run(attempt=1), QueryPending)
    h.warehouse.lookup_faults = ["unavailable"]

    outcome = await h.run(attempt=2)

    assert isinstance(outcome, QueryOutcomeUnknown)
    assert h.status() is S.OUTCOME_UNKNOWN


@pytest.mark.asyncio
async def test_job_reference_held_by_another_statement_is_never_released(
    h: Harness,
) -> None:
    h.warehouse.submit_faults = ["lost_response"]
    unknown = await h.run(attempt=1)
    assert isinstance(unknown, QueryOutcomeUnknown) and unknown.job is not None
    fake = h.warehouse.jobs[unknown.job.job_id]
    fake.submission = JobSubmission(
        fake.submission.ref,
        "SELECT 1",
        (),
        fake.submission.maximum_bytes_billed,
        "someone-elses-fingerprint",
    )

    outcome = failed(await h.run(attempt=2))
    assert outcome.code is ToolErrorCode.INTERNAL_ERROR
    assert outcome.reason == "job_reference_conflict"


@pytest.mark.asyncio
async def test_result_read_failure_retries_reading_the_same_job(h: Harness) -> None:
    h.warehouse.fetch_faults = ["unavailable"]
    outcome = failed(await h.run(attempt=1))
    assert outcome.retryable and h.status() is S.RETRYING

    succeeded(await h.run(attempt=2))
    assert len(h.warehouse.created) == 1


# --- pre-submission checks -----------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_estimate_over_query_cap_submits_nothing(h: Harness) -> None:
    h.warehouse.estimated_bytes = 2 * 1024**3

    outcome = failed(await h.run())

    assert outcome.code is ToolErrorCode.BUDGET_EXCEEDED
    assert h.jobs.rows == {}
    assert h.warehouse.submit_calls == 0
    assert h.status() is S.FAILED


@pytest.mark.asyncio
async def test_dry_run_rejection_and_transient_failure(h: Harness) -> None:
    h.warehouse.dry_run_faults = ["unavailable", "rejected:invalidQuery"]

    first = failed(await h.run(attempt=1))
    assert first.retryable and h.status() is S.RETRYING
    second = failed(await h.run(attempt=2))
    assert second.code is ToolErrorCode.INVALID_QUERY and second.correctable
    assert h.jobs.rows == {} and h.warehouse.submit_calls == 0


@pytest.mark.asyncio
async def test_admission_seam_sees_estimate_and_can_refuse(h: Harness) -> None:
    admission = RecordingAdmission(refuse=True)
    h.admission = admission

    outcome = failed(await h.run())

    assert outcome.code is ToolErrorCode.BUDGET_EXCEEDED
    assert admission.calls == [(OP, 1, h.warehouse.estimated_bytes)]
    assert h.jobs.rows == {}


@pytest.mark.asyncio
async def test_rejected_query_is_recorded_without_a_job(h: Harness) -> None:
    outcome = failed(
        await h.run(query="SELECT email FROM customers WHERE state = @status")
    )

    assert outcome.code in (
        ToolErrorCode.FIELD_UNAVAILABLE,
        ToolErrorCode.ACCESS_DENIED,
    )
    assert h.jobs.rows == {}
    assert h.status() is S.FAILED
    assert h.warehouse.dry_runs == []


# The rejected first attempt seen in real-model runs: a CTE joined to a relation.
DERIVED_JOIN = (
    "WITH target_year AS (SELECT MAX(EXTRACT(YEAR FROM ordered_date)) AS yr "
    "FROM sales_items) SELECT SUM(s.sale_amount) AS revenue FROM sales_items s "
    "JOIN target_year t ON EXTRACT(YEAR FROM s.ordered_date) = t.yr "
    "WHERE s.item_status = @status"
)


@pytest.mark.asyncio
async def test_compiler_rejection_issues_no_warehouse_job_or_query_charge(
    h: Harness,
) -> None:
    admission = RecordingAdmission()
    h.admission = admission

    outcome = failed(await h.run(query=DERIVED_JOIN))

    assert outcome.code is ToolErrorCode.UNSUPPORTED_SQL and outcome.correctable
    assert outcome.rejected
    # Nothing reached the warehouse and nothing was charged as a query.
    assert h.warehouse.dry_runs == [] and h.warehouse.submit_calls == 0
    assert h.warehouse.created == [] and h.jobs.rows == {}
    assert admission.calls is None
    record = h.operations.records[OP]
    assert record.status is S.FAILED and is_compiler_rejection(record)


@pytest.mark.asyncio
async def test_warehouse_refusal_is_a_failed_query_not_a_rejection(h: Harness) -> None:
    h.warehouse.dry_run_faults = ["rejected:invalidQuery"]

    outcome = failed(await h.run())

    assert outcome.code is ToolErrorCode.INVALID_QUERY and not outcome.rejected
    assert not is_compiler_rejection(h.operations.records[OP])


# --- authorization on every attempt -------------------------------------------


@pytest.mark.asyncio
async def test_authority_is_resolved_on_every_attempt_and_before_release(
    h: Harness,
) -> None:
    h.warehouse.submit_faults = ["lost_response"]
    await h.run(attempt=1)
    assert h.authority.resolutions == 1
    succeeded(await h.run(attempt=2))
    # Once at the start of attempt 2 and once more before releasing rows.
    assert h.authority.resolutions == 3


@pytest.mark.asyncio
async def test_revoked_access_between_attempts_releases_nothing(h: Harness) -> None:
    h.warehouse.submit_faults = ["lost_response"]
    await h.run(attempt=1)
    h.authority.deny = True

    outcome = failed(await h.run(attempt=2))

    assert outcome.code is ToolErrorCode.ACCESS_DENIED
    # Nothing was touched on behalf of an unauthorized caller.
    assert h.status() is S.OUTCOME_UNKNOWN


@pytest.mark.asyncio
async def test_access_revoked_while_the_job_ran_withholds_the_result(
    h: Harness,
) -> None:
    def revoke() -> None:
        h.authority.deny = True

    h.warehouse.after_finish = revoke

    outcome = failed(await h.run())

    assert outcome.code is ToolErrorCode.ACCESS_DENIED
    assert h.status() is S.FAILED


@pytest.mark.asyncio
async def test_entitlement_change_while_job_ran_withholds_the_stale_result(
    h: Harness,
) -> None:
    def narrow() -> None:
        h.authority.change_scope(1)

    h.warehouse.after_finish = narrow

    outcome = failed(await h.run())

    assert outcome.code is ToolErrorCode.ACCESS_DENIED
    assert outcome.reason.startswith("withheld_")


@pytest.mark.asyncio
async def test_entitlement_change_between_attempts_reruns_under_new_scope(
    h: Harness,
) -> None:
    h.warehouse.polls_to_finish = 1000
    pending = await h.run(attempt=1)
    assert isinstance(pending, QueryPending)
    h.authority.change_scope(1)  # product 3 revoked
    h.warehouse.polls_to_finish = 0

    outcome = succeeded(await h.run(attempt=2))

    old, new = h.jobs.rows[OP]
    assert h.warehouse.cancels == [old.job_id]
    assert new.authorization_version == SCOPE_A.entitlement_version + 1
    assert outcome.job == new
    assert outcome.result.entitlement_version == SCOPE_A.entitlement_version + 1


@pytest.mark.asyncio
async def test_unknown_executive_cannot_drive_someone_elses_operation(
    h: Harness,
) -> None:
    succeeded(await h.run())
    intruder = Principal("demo-b", frozenset({"analysis:read"}))
    outcome = await h.service().execute(
        QueryAttempt(intruder, RUN, OP, 2, AnalysisQuery(TOP_CUSTOMERS, VALUES))
    )
    assert failed(outcome).code is ToolErrorCode.ACCESS_DENIED


@pytest.mark.asyncio
async def test_catalog_outage_is_transient_and_touches_nothing(h: Harness) -> None:
    h.authority.catalog_down = True
    outcome = failed(await h.run())
    assert outcome.retryable
    assert h.operations.records == {}


@pytest.mark.asyncio
async def test_operation_from_another_run_is_refused(h: Harness) -> None:
    succeeded(await h.run())
    outcome = await h.service().execute(
        QueryAttempt(PRINCIPAL, "run-2", OP, 2, AnalysisQuery(TOP_CUSTOMERS, VALUES))
    )
    assert failed(outcome).reason == "operation_not_in_run"


@pytest.mark.asyncio
async def test_stale_attempt_is_superseded(h: Harness) -> None:
    h.warehouse.submit_faults = ["lost_response"]
    await h.run(attempt=1)
    await h.run(attempt=3)

    outcome = await h.run(attempt=2)

    assert isinstance(outcome, QueryOutcomeUnknown)
    assert outcome.reason == "superseded_attempt"


# --- progress wording (T22-F6) -------------------------------------------------


@pytest.mark.asyncio
async def test_describe_compiles_without_any_record_or_warehouse_call(
    h: Harness,
) -> None:
    from retail_analytics.capabilities.analysis import query_progress_label

    service = h.service()
    compiled = await service.describe(
        QueryAttempt(PRINCIPAL, RUN, OP, 1, AnalysisQuery(TOP_CUSTOMERS, VALUES))
    )
    assert compiled is not None
    assert query_progress_label(compiled) == "Calculating revenue."
    rejected = await service.describe(
        QueryAttempt(PRINCIPAL, RUN, OP, 1, AnalysisQuery("DELETE FROM x", {}))
    )
    assert rejected is None
    assert h.operations.records == {}
    assert h.warehouse.submit_calls == 0 and h.warehouse.dry_runs == []
