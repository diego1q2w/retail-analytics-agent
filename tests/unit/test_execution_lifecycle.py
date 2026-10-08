"""Pure lifecycle rules for runs and tool executions."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from retail_analytics.domain.errors import InvalidTransition
from retail_analytics.domain.executions import (
    ToolExecution,
    ToolExecutionStatus,
    query_job_id,
)
from retail_analytics.domain.operations import SideEffect, ToolErrorCode
from retail_analytics.domain.runs import Run, RunStatus

T0 = datetime(2026, 10, 1, tzinfo=UTC)
T1 = T0 + timedelta(seconds=5)
S = ToolExecutionStatus


def _run(status: RunStatus = RunStatus.RUNNING) -> Run:
    return Run(
        run_id="run-1",
        session_id="ses-1",
        requested_by="exec-1",
        trigger_message_id="msg-1",
        submission_key="k1",
        status=status,
        created_at=T0,
        updated_at=T0,
    )


def _op(status: ToolExecutionStatus = S.PREPARED, attempts: int = 0) -> ToolExecution:
    return ToolExecution(
        operation_id="op-1",
        run_id="run-1",
        capability="run_query",
        capability_version=1,
        side_effect=SideEffect.EXTERNAL_JOB,
        status=status,
        attempt_count=attempts,
        created_at=T0,
        updated_at=T0,
    )


def test_only_running_waiting_and_cancelling_runs_are_active() -> None:
    active = {status for status in RunStatus if status.is_active}
    assert active == {
        RunStatus.RUNNING,
        RunStatus.WAITING_FOR_INPUT,
        RunStatus.CANCELLING,
    }


def test_completing_a_run_stamps_completion_time() -> None:
    done = _run().transition(RunStatus.COMPLETED, at=T1)
    assert done is not None
    assert (done.status, done.completed_at, done.updated_at) == (
        RunStatus.COMPLETED,
        T1,
        T1,
    )


def test_run_transition_to_current_status_is_a_no_op() -> None:
    assert _run().transition(RunStatus.RUNNING, at=T1) is None


@pytest.mark.parametrize("terminal", [s for s in RunStatus if s.is_terminal])
def test_terminal_runs_cannot_be_reopened(terminal: RunStatus) -> None:
    with pytest.raises(InvalidTransition):
        _run(terminal).transition(RunStatus.RUNNING, at=T1)


def test_clarification_wait_resumes_into_running() -> None:
    waiting = _run().transition(RunStatus.WAITING_FOR_INPUT, at=T1)
    assert waiting is not None
    resumed = waiting.transition(RunStatus.RUNNING, at=T1)
    assert resumed is not None
    assert resumed.completed_at is None


def test_operation_follows_reconcile_first_path() -> None:
    op = _op()
    for to in (S.SUBMITTING, S.OUTCOME_UNKNOWN, S.RUNNING, S.SUCCEEDED):
        nxt = op.transition(to, attempt=1, at=T1)
        assert nxt is not None
        op = nxt
    assert (op.status, op.attempt_count) == (S.SUCCEEDED, 1)


def test_repeating_a_reported_transition_is_a_no_op() -> None:
    running = _op().transition(S.RUNNING, attempt=1, at=T1)
    assert running is not None
    assert running.transition(S.RUNNING, attempt=1, at=T1) is None


def test_retry_increments_attempt_through_retrying() -> None:
    op = _op(S.RUNNING, attempts=1)
    retrying = op.transition(
        S.RETRYING, attempt=1, at=T1, error_code=ToolErrorCode.TEMPORARY_FAILURE
    )
    assert retrying is not None
    again = retrying.transition(S.RUNNING, attempt=2, at=T1)
    assert again is not None
    assert again.attempt_count == 2
    with pytest.raises(InvalidTransition):
        _op(S.RUNNING, attempts=1).transition(S.RUNNING, attempt=2, at=T1)


def test_reports_from_an_older_attempt_are_rejected() -> None:
    with pytest.raises(InvalidTransition):
        _op(S.RUNNING, attempts=2).transition(S.SUCCEEDED, attempt=1, at=T1)


@pytest.mark.parametrize("terminal", [S.SUCCEEDED, S.FAILED, S.CANCELLED])
def test_terminal_operations_are_final(terminal: ToolExecutionStatus) -> None:
    with pytest.raises(InvalidTransition):
        _op(terminal, attempts=1).transition(S.RUNNING, attempt=1, at=T1)


def test_failure_requires_and_records_an_error_code() -> None:
    with pytest.raises(ValueError, match="error code"):
        _op().transition(S.FAILED, attempt=1, at=T1)
    with pytest.raises(ValueError, match="does not carry"):
        _op().transition(
            S.SUCCEEDED, attempt=1, at=T1, error_code=ToolErrorCode.INTERNAL_ERROR
        )
    failed = _op().transition(
        S.FAILED,
        attempt=1,
        at=T1,
        error_code=ToolErrorCode.BUDGET_EXCEEDED,
        detail="query budget exhausted",
    )
    assert failed is not None
    assert failed.error_code is ToolErrorCode.BUDGET_EXCEEDED
    assert failed.error_detail == "query budget exhausted"


def test_transient_failure_before_submission_is_recorded_as_retrying() -> None:
    retrying = _op().transition(
        S.RETRYING, attempt=1, at=T1, error_code=ToolErrorCode.TEMPORARY_FAILURE
    )
    assert retrying is not None
    again = retrying.transition(
        S.RETRYING, attempt=2, at=T1, error_code=ToolErrorCode.TEMPORARY_FAILURE
    )
    assert again is not None and again.attempt_count == 2
    assert again.transition(S.RETRYING, attempt=2, at=T1) is None


def test_query_job_ids_are_deterministic_per_operation_and_submission() -> None:
    first = query_job_id("ra", "op-1", 1)
    assert first == query_job_id("ra", "op-1", 1)
    assert first != query_job_id("ra", "op-1", 2)
    assert first != query_job_id("ra", "op-2", 1)
    assert first != query_job_id("other", "op-1", 1)
    assert "op-1" not in first
    assert re.fullmatch(r"ra_[0-9a-f]{40}_1", first)
    with pytest.raises(ValueError):
        query_job_id("Bad-Namespace", "op-1", 1)
    with pytest.raises(ValueError):
        query_job_id("ra", "op-1", 0)
