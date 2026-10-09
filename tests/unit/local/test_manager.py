"""The local manager carries out the shared lifecycle decisions in-process.

Controlled runtime steps and the shared agent over a scripted provider; the
PostgreSQL behaviour is covered by ``tests/integration/test_local_*``.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from retail_analytics.adapters.local.investigations import (
    ManagerNotBound,
    interruption,
)
from retail_analytics.application.contracts.investigations import (
    InterruptionKind,
    StopReason,
)
from retail_analytics.application.contracts.persistence import IdempotencyConflict
from retail_analytics.application.investigation_runtime import (
    InvestigationContextChanged,
    RunStopped,
)
from retail_analytics.domain.budgets import BudgetResource
from retail_analytics.domain.runs import ExecutionBackend, RunStatus
from tests.unit.local.fakes import Harness, LockHeld, harness


async def _waiting(h: Harness, run_id: str) -> None:
    async with asyncio.timeout(5):
        while True:
            if h.runtime.status.get(run_id) is RunStatus.WAITING_FOR_INPUT:
                return
            await asyncio.sleep(0.01)


async def _settled(manager: object, *, seconds: float = 5) -> None:
    async with asyncio.timeout(seconds):
        while True:
            if not manager.running():  # type: ignore[attr-defined]
                return
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_answer_and_duplicate_start_execute_once() -> None:
    h = harness()
    await h.manager.open()
    try:
        first = await h.manager.start("r1")
        second = await h.manager.start("r1")
        assert first == second and first.backend is ExecutionBackend.LOCAL
        assert first.workflow_id == h.manager.instance_id
        assert h.manager.running() == {"r1"}
        await _settled(h.manager)
        assert h.runtime.steps("r1") == ["begin", "release_answer"]
        assert [a.text for a in h.runtime.answers] == ["Sales grew 4%."]
        # A later start of a run this manager executed never runs it again.
        await h.manager.start("r1")
        await asyncio.sleep(0.05)
        assert h.runtime.steps("r1") == ["begin", "release_answer"]
        assert h.sweep.owners == [h.manager.instance_id]
    finally:
        await h.manager.close()
    assert not h.lock.held


@pytest.mark.asyncio
async def test_clarification_waits_without_model_calls_until_notified() -> None:
    h = harness(ends_with="clarify")
    await h.manager.open()
    try:
        await h.manager.start("r1")
        await _waiting(h, "r1")
        await asyncio.sleep(0.05)
        requests = len(h.agent.provider.requests)
        # A wake-up without persisted input changes nothing.
        await h.manager.notify_input("r1")
        await asyncio.sleep(0.2)
        assert len(h.agent.provider.requests) == requests
        assert h.runtime.steps("r1")[-1] == "resume"
        h.runtime.add_input("r1")
        await h.manager.notify_input("r1")
        await _settled(h.manager)
        assert h.runtime.status["r1"] is RunStatus.COMPLETED
        assert h.runtime.steps("r1")[:2] == ["begin", "ask"]
        assert len(h.agent.provider.requests) > requests
    finally:
        await h.manager.close()


@pytest.mark.asyncio
async def test_cancel_while_waiting_settles_through_cancel_steps() -> None:
    h = harness(ends_with="clarify")
    await h.manager.open()
    try:
        await h.manager.start("r1")
        await _waiting(h, "r1")
        h.runtime.cancel("r1")
        await h.manager.request_cancel("r1")
        await _settled(h.manager)
        assert h.runtime.steps("r1")[-2:] == ["begin_cancel", "finish_cancelled"]
        assert h.runtime.status["r1"] is RunStatus.CANCELLED
    finally:
        await h.manager.close()


@pytest.mark.asyncio
async def test_wait_limit_expires_the_clarification() -> None:
    h = harness(ends_with="clarify", wait_limit=timedelta(milliseconds=50))
    await h.manager.open()
    try:
        await h.manager.start("r1")
        await _settled(h.manager)
        assert h.runtime.steps("r1")[-1] == "expire"
    finally:
        await h.manager.close()


@pytest.mark.asyncio
async def test_step_failure_stops_the_run_without_retrying() -> None:
    h = harness()
    h.runtime.fail_release = True
    await h.manager.open()
    try:
        await h.manager.start("r1")
        await _settled(h.manager)
        assert h.runtime.steps("r1") == [
            "begin",
            "release_answer",
            "finish_partial:interrupted",
        ]
    finally:
        await h.manager.close()


@pytest.mark.asyncio
async def test_close_interrupts_owned_work_and_refuses_new_work() -> None:
    h = harness(ends_with="clarify")
    await h.manager.open()
    await h.manager.start("waiting")
    await _waiting(h, "waiting")
    await h.manager.close()
    assert not h.manager.admitting and not h.lock.held
    assert h.manager.running() == frozenset()
    assert h.runtime.steps("waiting")[-1] == "interrupt"
    assert h.runtime.status["waiting"] is RunStatus.FAILED
    # Started after shutdown (for example a queued promotion): ended at once.
    await h.manager.start("late")
    assert h.runtime.steps("late") == ["interrupt"]
    assert h.manager.running() == frozenset()


@pytest.mark.asyncio
async def test_other_backend_runs_are_refused() -> None:
    h = harness()
    h.runs.temporal.add("temporal-run")
    await h.manager.open()
    try:
        with pytest.raises(IdempotencyConflict):
            await h.manager.start("temporal-run")
        assert h.manager.running() == frozenset()
        assert h.runtime.calls == []
    finally:
        await h.manager.close()


@pytest.mark.asyncio
async def test_open_fails_clearly_when_another_manager_holds_the_lock() -> None:
    h = harness()
    h.lock.held_elsewhere = True
    with pytest.raises(LockHeld):
        await h.manager.open()
    assert not h.manager.admitting and h.sweep.owners == []


@pytest.mark.asyncio
async def test_unbound_manager_never_executes() -> None:
    h = harness()
    h.manager._bound = None
    with pytest.raises(ManagerNotBound):
        await h.manager.open()
    with pytest.raises(ManagerNotBound):
        await h.manager.start("r1")


def test_interruptions_map_through_wrappers() -> None:
    stopped = RunStopped(StopReason.BUDGET, BudgetResource.QUERIES)
    group = ExceptionGroup("agent", [RuntimeError("x"), stopped])
    mapped = interruption(group)
    assert mapped.kind is InterruptionKind.STOPPED
    assert mapped.reason is StopReason.BUDGET
    assert mapped.resource is BudgetResource.QUERIES
    try:
        try:
            raise InvestigationContextChanged
        except InvestigationContextChanged as error:
            raise RuntimeError("wrapped") from error
    except RuntimeError as wrapped:
        changed = interruption(wrapped)
    assert changed.kind is InterruptionKind.CONTEXT_CHANGED
    assert interruption(ValueError("provider")).kind is InterruptionKind.FAILED
