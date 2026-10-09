"""Cleanup after a stop (T11-F1): a still-running warehouse job gets one
bounded cancellation request by its recorded job ID, never a resubmission."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from typing import Any

import pytest
from duckdb import DuckDBPyConnection as Connection

from retail_analytics.application import investigation_runtime
from retail_analytics.application.investigation_runtime import InvestigationRuntime
from retail_analytics.application.query_execution import QueryPending
from retail_analytics.domain.executions import ToolExecutionStatus
from tests.unit.privacy.support import customer_database
from tests.unit.query_execution.test_service import OP, RUN, Harness

S = ToolExecutionStatus


@pytest.fixture
def h() -> Iterator[Harness]:
    connection: Connection = customer_database()
    yield Harness(connection)
    connection.close()


async def _running(h: Harness) -> InvestigationRuntime:
    h.warehouse.polls_to_finish = 10**6
    assert isinstance(await h.run(attempt=1), QueryPending)
    assert h.status() is S.RUNNING
    runtime = InvestigationRuntime.__new__(InvestigationRuntime)
    runtime._operations = h.operations
    runtime._queries = h.service()
    return runtime


@pytest.mark.asyncio
async def test_running_job_is_cancelled_by_its_recorded_id(h: Harness) -> None:
    runtime = await _running(h)
    cleanup = await runtime._stop_operations(RUN)
    assert (cleanup.requested, cleanup.unconfirmed) == (1, 0)
    assert h.warehouse.cancels == h.warehouse.created  # the recorded job
    assert h.warehouse.submit_calls == 1  # nothing resubmitted
    assert h.status() is S.CANCELLED


@pytest.mark.asyncio
async def test_unconfirmed_cancellation_is_reported_not_awaited(h: Harness) -> None:
    runtime = await _running(h)
    h.warehouse.cancel_takes_effect = False
    cleanup = await runtime._stop_operations(RUN)
    assert (cleanup.requested, cleanup.unconfirmed) == (1, 1)
    assert h.status() is S.CANCEL_REQUESTED
    assert h.warehouse.submit_calls == 1


@pytest.mark.asyncio
async def test_cleanup_is_bounded_when_the_warehouse_does_not_answer(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = await _running(h)

    async def hanging(ref: Any) -> None:
        await asyncio.sleep(30)

    monkeypatch.setattr(h.warehouse, "cancel", hanging)
    monkeypatch.setattr(investigation_runtime, "CLEANUP_SECONDS", 0.2)
    started = time.monotonic()
    cleanup = await runtime._stop_operations(RUN)
    assert time.monotonic() - started < 2
    assert (cleanup.requested, cleanup.unconfirmed) == (1, 1)
    assert h.warehouse.submit_calls == 1
    assert OP in h.operations.records
