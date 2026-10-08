"""Query execution under run limits: deadline, transient attempts, budgets,
truncation and the recovery classification, with a fake clock.

Every ``h.run`` builds a new service instance over the same records, as a
restarted worker would, so nothing here can be reset by a retry.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from duckdb import DuckDBPyConnection as Connection

from retail_analytics.application.budgets import RunBudgets
from retail_analytics.application.contracts.query_compiler import AnalysisQuery
from retail_analytics.application.contracts.result_privacy import QueryRows
from retail_analytics.application.query_execution import (
    QUERY_DEADLINE,
    RETRIES_EXHAUSTED,
    QueryAttempt,
    QueryExecutionSettings,
    QueryFailed,
    QueryOutcome,
    QueryPending,
)
from retail_analytics.application.recovery import RecoveryAction, classify
from retail_analytics.application.result_privacy import (
    ResultLimits,
    ResultPrivacyBoundary,
    TruncationReason,
)
from retail_analytics.domain.budgets import RunLimits
from retail_analytics.domain.executions import ToolExecutionStatus
from retail_analytics.domain.operations import ToolErrorCode
from tests.unit.budgets.memory_store import MemoryRunBudgetStore
from tests.unit.privacy.support import (
    EXEC_A,
    SCOPE_A,
    compile_for,
    customer_database,
)
from tests.unit.query_execution.fakes import CrashAfterSubmit
from tests.unit.query_execution.test_service import (
    OP,
    PRINCIPAL,
    RUN,
    TOP_CUSTOMERS,
    Harness,
    failed,
    succeeded,
)
from tests.unit.sql_compiler.support import view

S = ToolExecutionStatus
MIB = 1024 * 1024


@pytest.fixture
def h() -> Iterator[Harness]:
    connection: Connection = customer_database()
    yield Harness(connection)
    connection.close()


def _with_budgets(h: Harness, limits: RunLimits | None = None) -> RunBudgets:
    budgets = RunBudgets(
        MemoryRunBudgetStore(), limits or RunLimits(), clock=h.clock.wall
    )
    h.admission = budgets
    h.usage = budgets
    return budgets


async def _until_not_pending(h: Harness, start: int = 1) -> tuple[QueryOutcome, int]:
    attempt = start
    outcome = await h.run(attempt=attempt)
    while isinstance(outcome, QueryPending):
        attempt += 1
        outcome = await h.run(attempt=attempt)
    return outcome, attempt


# --- 2-minute query deadline ---------------------------------------------------


@pytest.mark.asyncio
async def test_deadline_requests_cancellation_and_releases_nothing(
    h: Harness,
) -> None:
    h.warehouse.polls_to_finish = 10**6

    outcome, attempts = await _until_not_pending(h)

    result = failed(outcome)
    assert (result.code, result.reason) == (
        ToolErrorCode.BUDGET_EXCEEDED,
        QUERY_DEADLINE,
    )
    assert result.stopping is None and not result.retryable
    assert h.clock.now == 120.0  # waits were cut at the deadline, not after it
    assert attempts == 12  # 10-second attempt waits
    job_id = h.warehouse.created[0]
    assert h.warehouse.cancels == [job_id]
    assert h.warehouse.jobs[job_id].cancelled
    assert h.statuses()[-2:] == [S.CANCEL_REQUESTED, S.FAILED]
    assert h.operations.records[OP].error_detail == QUERY_DEADLINE
    assert classify(result).action is RecoveryAction.NARROW

    # A later attempt reports the same failure and submits nothing.
    again = failed(await h.run(attempt=attempts + 1))
    assert again.reason == QUERY_DEADLINE
    assert h.warehouse.submit_calls == 1


@pytest.mark.asyncio
async def test_deadline_with_slow_cancellation_reconciles_before_finishing(
    h: Harness,
) -> None:
    h.warehouse.polls_to_finish = 10**6
    h.warehouse.cancel_takes_effect = False

    outcome, attempts = await _until_not_pending(h)

    stopping = failed(outcome)
    assert stopping.reason == QUERY_DEADLINE and stopping.stopping is not None
    assert h.status() is S.CANCEL_REQUESTED
    assert classify(stopping).action is RecoveryAction.RECONCILE

    # Still running: another attempt keeps reconciling, never resubmits.
    still = failed(await h.run(attempt=attempts + 1))
    assert still.stopping is not None
    assert h.warehouse.submit_calls == 1

    h.warehouse.finish(h.warehouse.created[0])
    final = failed(await h.service().reconcile_cancel(RUN, OP))
    assert (final.reason, final.stopping) == (QUERY_DEADLINE, None)
    assert h.status() is S.FAILED
    assert len(h.warehouse.created) == 1


@pytest.mark.asyncio
async def test_deadline_survives_a_worker_restart(h: Harness) -> None:
    h.warehouse.polls_to_finish = 10**6
    assert isinstance(await h.run(attempt=1), QueryPending)

    h.clock.now += 3600  # the worker came back an hour later
    result = failed(await h.run(attempt=2))

    assert result.reason == QUERY_DEADLINE
    assert h.warehouse.submit_calls == 1
    assert h.warehouse.cancels == h.warehouse.created


@pytest.mark.asyncio
async def test_deadline_before_any_submission_submits_nothing(h: Harness) -> None:
    h.warehouse.dry_run_faults = ["unavailable"]
    assert failed(await h.run(attempt=1)).retryable

    h.clock.now += 121
    result = failed(await h.run(attempt=2))

    assert result.reason == QUERY_DEADLINE
    assert h.warehouse.created == [] and h.warehouse.cancels == []


@pytest.mark.asyncio
async def test_job_carries_a_warehouse_timeout_backstop(h: Harness) -> None:
    succeeded(await h.run())
    job = h.warehouse.jobs[h.warehouse.created[0]]
    assert job.submission.timeout_seconds == 150.0
    record = h.operations.records[OP]
    assert record.deadline_at is not None
    assert (record.deadline_at - record.created_at).total_seconds() == 120


# --- three total transient attempts ----------------------------------------------


@pytest.mark.asyncio
async def test_third_transient_job_failure_stops_the_operation(h: Harness) -> None:
    h.warehouse.job_errors = ["backendError"] * 5

    first = failed(await h.run(attempt=1))
    second = failed(await h.run(attempt=2))
    third = failed(await h.run(attempt=3))

    assert first.retryable and second.retryable
    assert classify(first).action is RecoveryAction.RETRY
    assert (third.reason, third.retryable) == (RETRIES_EXHAUSTED, False)
    assert third.code is ToolErrorCode.TEMPORARY_FAILURE
    assert classify(third).action is RecoveryAction.STOP
    assert len(h.warehouse.created) == 3
    assert h.status() is S.FAILED

    after = failed(await h.run(attempt=4))
    assert after.reason == RETRIES_EXHAUSTED
    assert len(h.warehouse.created) == 3


@pytest.mark.asyncio
async def test_transient_pre_submission_failures_are_capped(h: Harness) -> None:
    h.warehouse.dry_run_faults = ["unavailable"] * 5

    outcomes = [failed(await h.run(attempt=n)) for n in (1, 2, 3)]

    assert [o.retryable for o in outcomes] == [True, True, False]
    assert outcomes[-1].reason == RETRIES_EXHAUSTED
    assert h.warehouse.created == []


@pytest.mark.asyncio
async def test_submissions_are_capped_even_when_failures_went_unrecorded(
    h: Harness,
) -> None:
    # The worker dies right after each submission, so no attempt ever records
    # the job's transient failure; the submission cap still holds.
    h.warehouse.job_errors = ["backendError"] * 5
    h.warehouse.submit_faults = ["crash"] * 5
    for attempt in (1, 2, 3):
        with pytest.raises(CrashAfterSubmit):
            await h.run(attempt=attempt)

    result = failed(await h.run(attempt=4))

    assert result.reason == RETRIES_EXHAUSTED and not result.retryable
    assert len(h.warehouse.created) == 3


@pytest.mark.asyncio
async def test_retry_limit_is_configurable(h: Harness) -> None:
    h.settings = QueryExecutionSettings(
        project="test-project", location="US", max_transient_attempts=1
    )
    h.warehouse.job_errors = ["backendError"]
    assert failed(await h.run()).reason == RETRIES_EXHAUSTED


# --- run budgets through admission and settlement --------------------------------


@pytest.mark.asyncio
async def test_eleventh_query_of_a_run_is_refused_before_submission(
    h: Harness,
) -> None:
    budgets = _with_budgets(h)
    for n in range(10):
        succeeded(await h.run(op=f"op-{n:04d}"))
        succeeded(await h.run(op=f"op-{n:04d}", attempt=2))  # retry: no charge

    refused = failed(await h.run(op="op-0010"))

    assert refused.code is ToolErrorCode.BUDGET_EXCEEDED
    assert refused.reason == "run_budget_queries"
    assert classify(refused).action is RecoveryAction.STOP
    assert len(h.warehouse.created) == 10
    assert "op-0010" not in h.jobs.rows
    snapshot = await budgets.snapshot(RUN)
    assert snapshot is not None and snapshot.usage.queries == 10


@pytest.mark.asyncio
async def test_resubmission_after_a_transient_failure_is_another_query(
    h: Harness,
) -> None:
    budgets = _with_budgets(h, RunLimits(queries=2))
    h.warehouse.job_errors = ["backendError"]
    assert failed(await h.run(attempt=1)).retryable
    succeeded(await h.run(attempt=2))

    snapshot = await budgets.snapshot(RUN)
    assert snapshot is not None and snapshot.usage.queries == 2
    assert failed(await h.run(op="op-0002")).reason == "run_budget_queries"


@pytest.mark.asyncio
async def test_run_scan_budget_settles_billed_bytes(h: Harness) -> None:
    # The fake bills 10 MiB per job whatever the 1 KiB estimate says.
    budgets = _with_budgets(
        h, RunLimits(bytes_per_query=20 * MIB, bytes_per_run=25 * MIB)
    )
    for n in range(3):
        succeeded(await h.run(op=f"op-{n:04d}"))
    snapshot = await budgets.snapshot(RUN)
    assert snapshot is not None and snapshot.usage.bytes == 30 * MIB

    refused = failed(await h.run(op="op-0003"))

    assert refused.reason == "run_budget_run_bytes"
    assert len(h.warehouse.created) == 3


@pytest.mark.asyncio
async def test_per_query_scan_limit_asks_for_a_narrower_query(h: Harness) -> None:
    _with_budgets(h, RunLimits(bytes_per_query=MIB, bytes_per_run=5 * MIB))
    h.warehouse.estimated_bytes = MIB + 1

    refused = failed(await h.run())

    assert refused.reason == "query_budget_query_bytes"
    assert classify(refused).action is RecoveryAction.NARROW
    assert h.warehouse.created == []


@pytest.mark.asyncio
async def test_deadline_settles_the_stopped_jobs_usage(h: Harness) -> None:
    budgets = _with_budgets(h)
    h.warehouse.polls_to_finish = 10**6
    outcome, _ = await _until_not_pending(h)
    assert failed(outcome).reason == QUERY_DEADLINE
    snapshot = await budgets.snapshot(RUN)
    assert snapshot is not None and snapshot.usage.bytes == 10 * MIB


@pytest.mark.asyncio
async def test_run_time_budget_refuses_new_queries(h: Harness) -> None:
    budgets = _with_budgets(h)
    await budgets.open(RUN)
    h.clock.now += 600

    refused = failed(await h.run())

    assert refused.reason == "run_budget_active_time"
    assert h.warehouse.created == []


# --- truncation, empty results and classification --------------------------------


@pytest.mark.asyncio
async def test_truncated_result_is_never_complete(h: Harness) -> None:
    h.limits = ResultLimits(max_rows=2)
    h.settings = QueryExecutionSettings(
        project="test-project", location="US", max_rows=2
    )
    outcome = succeeded(await h.run())
    assert outcome.result.truncated
    recovery = classify(outcome)
    assert (recovery.action, recovery.complete, recovery.truncated) == (
        RecoveryAction.DONE,
        False,
        True,
    )


@pytest.mark.asyncio
async def test_complete_and_empty_results(h: Harness) -> None:
    full = classify(succeeded(await h.run()))
    assert (full.action, full.complete) == (RecoveryAction.DONE, True)

    attempt = QueryAttempt(
        PRINCIPAL,
        RUN,
        "op-empty",
        1,
        AnalysisQuery(TOP_CUSTOMERS, {"status": "NoSuchStatus"}),
    )
    empty = succeeded(await h.service().execute(attempt))
    assert empty.result.rows == () and not empty.result.truncated
    assert classify(empty).action is RecoveryAction.EMPTY


def test_default_result_caps_flag_rows_and_bytes() -> None:
    assert ResultLimits() == ResultLimits(500, 256 * 1024)
    boundary = ResultPrivacyBoundary()
    compiled = compile_for(EXEC_A, "SELECT product_name FROM products", SCOPE_A)
    catalog = view(version=SCOPE_A.entitlement_version)

    many = boundary.release(
        compiled, QueryRows(("product_name",), [("x",)] * 501), catalog=catalog
    )
    assert len(many.rows) == 500 and many.truncation is TruncationReason.ROWS

    wide = boundary.release(
        compiled, QueryRows(("product_name",), [("y" * 1000,)] * 400), catalog=catalog
    )
    size = sum(len(str(row[0])) + 6 for row in wide.rows)  # value + separators
    assert wide.truncation is TruncationReason.BYTES
    assert size <= 256 * 1024 < size + 1006
    assert len(wide.rows) < 400


def test_failures_classify_to_the_agreed_recovery() -> None:
    def action(code: ToolErrorCode, reason: str = "x") -> RecoveryAction:
        return classify(QueryFailed(code, reason, "m")).action

    assert action(ToolErrorCode.INVALID_QUERY) is RecoveryAction.REFORMULATE
    assert action(ToolErrorCode.UNSUPPORTED_SQL) is RecoveryAction.REFORMULATE
    assert action(ToolErrorCode.FIELD_UNAVAILABLE) is RecoveryAction.REFORMULATE
    assert action(ToolErrorCode.ACCESS_DENIED) is RecoveryAction.STOP
    assert action(ToolErrorCode.INTERNAL_ERROR) is RecoveryAction.STOP
    assert (
        action(ToolErrorCode.BUDGET_EXCEEDED, "estimate_over_query_limit")
        is RecoveryAction.NARROW
    )
    assert (
        action(ToolErrorCode.BUDGET_EXCEEDED, "job_bytesBilledLimitExceeded")
        is RecoveryAction.NARROW
    )
    assert action(ToolErrorCode.BUDGET_EXCEEDED, "job_quotaExceeded") is (
        RecoveryAction.STOP
    )
    assert action(ToolErrorCode.BUDGET_EXCEEDED, "run_budget_queries") is (
        RecoveryAction.STOP
    )
