"""The active-time deadline interrupts in-flight work (T11-F1).

Short limits and fake clocks: the run's remaining allowance is read from
accounting (a fake clock placed just before the limit), so the real waits
here are fractions of a second, never the 120-second default.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, FunctionModel

from retail_analytics.adapters.agent.investigator import (
    AgentBinding,
    AgentServices,
    build_investigation_agent,
    run_investigation,
)
from retail_analytics.adapters.models.deadlines import ResponseLimits, StreamDeadlines
from retail_analytics.application.budgets import (
    ActiveDeadlineReached,
    RetrySettings,
    RunBudgets,
    within_active_time,
)
from retail_analytics.application.contracts import Correlation
from retail_analytics.application.contracts.investigations import StopReason
from retail_analytics.application.investigation_runtime import RunStopped
from retail_analytics.application.tool_runner import ToolRunner
from retail_analytics.application.tools import (
    AuthorizationSpec,
    CapabilityRegistry,
    CapabilitySpec,
    ExecutionContext,
    OperationContext,
    RetrySpec,
    ToolFailed,
    ToolInput,
    ToolOutput,
    ToolSucceeded,
)
from retail_analytics.domain.access import ProductScope
from retail_analytics.domain.budgets import BudgetResource, RunLimits
from retail_analytics.domain.operations import RecoveryMode, SideEffect, ToolErrorCode
from retail_analytics.domain.runs import RunStatus
from tests.unit.agent.scenario import STEP, FakeSteps, FakeTools
from tests.unit.budgets.memory_store import MemoryRunBudgetStore
from tests.unit.budgets.test_run_budgets import RUN, Clock
from tests.unit.query_execution.fakes import MemoryOperations
from tests.unit.tools.fakes import RecordingSink as ProgressSink

pytestmark = pytest.mark.asyncio
# Real seconds a test may wait for the cut-off (the allowance plus slack).
SLACK = 2.0


# The helper


async def test_work_that_finishes_in_time_is_returned() -> None:
    async def quick() -> str:
        await asyncio.sleep(0)
        return "done"

    assert await within_active_time(1.0, quick()) == "done"
    assert await within_active_time(None, quick()) == "done"


async def test_slow_work_is_cut_off_at_the_deadline() -> None:
    started = time.monotonic()
    with pytest.raises(ActiveDeadlineReached) as reached:
        await within_active_time(0.1, asyncio.sleep(30))
    assert reached.value.resource is BudgetResource.ACTIVE_TIME
    assert reached.value.run_exhausted
    assert time.monotonic() - started < SLACK


async def test_spent_allowance_starts_nothing() -> None:
    started: list[str] = []

    async def work() -> None:
        started.append("ran")

    with pytest.raises(ActiveDeadlineReached):
        await within_active_time(0.0, work())
    assert started == []


async def test_a_timeout_inside_the_work_is_not_mistaken_for_the_deadline() -> None:
    async def own_timeout() -> None:
        async with asyncio.timeout(0.01):
            await asyncio.sleep(1)

    with pytest.raises(TimeoutError):
        await within_active_time(30, own_timeout())


# Accounting: clarification waits excluded, nothing resets the allowance


async def test_clarification_pause_is_free_and_recovery_never_resets() -> None:
    store, clock = MemoryRunBudgetStore(), Clock()
    budgets = RunBudgets(store, RunLimits(), clock=clock)
    assert (await budgets.open(RUN)).limits.active_seconds == 120
    clock.advance(70)
    await budgets.pause_for_clarification(RUN)
    clock.advance(3600)  # the user answers an hour later
    await budgets.resume_after_clarification(RUN)
    clock.advance(30)
    # A reconnect, an activity retry or a restarted worker opens again with
    # other settings: same pinned limit, same usage.
    restarted = RunBudgets(store, RunLimits(active_seconds=600), clock=clock)
    reopened = await restarted.open(RUN)
    assert reopened.limits.active_seconds == 120
    assert reopened.remaining()[BudgetResource.ACTIVE_TIME] == 20
    clock.advance(20)
    decision = await restarted.retry_decision(RUN, 1)
    assert not decision.allowed
    assert decision.exhausted is BudgetResource.ACTIVE_TIME


# The model request


async def _endless_stream(
    messages: list[ModelMessage], info: AgentInfo
) -> AsyncIterator[str]:
    # A stream that keeps progressing: no first-token or stall limit fires.
    while True:
        yield "still thinking "
        await asyncio.sleep(0.02)


async def test_a_progressing_model_stream_is_cut_off_at_the_deadline() -> None:
    steps = FakeSteps((replace(STEP, active_seconds_left=0.3),))
    provider = StreamDeadlines(
        FunctionModel(stream_function=_endless_stream, model_name="slow"),
        ResponseLimits(first_token_seconds=60, stall_seconds=30, total_seconds=180),
    )
    agent = build_investigation_agent(
        AgentBinding(AgentServices(steps, FakeTools(), provider))
    )
    started = time.monotonic()
    with pytest.raises(RunStopped) as stopped:
        await run_investigation(agent, "r")
    assert stopped.value.reason is StopReason.BUDGET
    assert stopped.value.resource is BudgetResource.ACTIVE_TIME
    assert time.monotonic() - started < SLACK
    assert steps.calls == 1  # no further model request was prepared


# The tool call


class _In(ToolInput):
    pass


class _Out(ToolOutput):
    done: bool


def _spec(handler: Any, recovery: RecoveryMode) -> CapabilitySpec[Any, Any]:
    return CapabilitySpec(
        name="slow_tool",
        version=1,
        description="slow",
        progress_label="slow",
        input_model=_In,
        output_model=_Out,
        handler=handler,
        authorization=AuthorizationSpec(required_permissions=frozenset()),
        side_effect=SideEffect.READ_ONLY,
        retry=RetrySpec(
            recovery,
            1 if recovery is RecoveryMode.NO_RETRY else 3,
            timedelta(seconds=5),
        ),
    )


async def _runner(
    spec: CapabilitySpec[Any, Any],
    *,
    seconds_left: float,
    sleep: Any = asyncio.sleep,
) -> ToolRunner:
    clock = Clock()
    budgets = RunBudgets(
        MemoryRunBudgetStore(),
        RunLimits(),
        retry=RetrySettings(1.0, 20.0),
        clock=clock,
        jitter=lambda: 0.0,
    )
    await budgets.open("r")
    clock.advance(120 - seconds_left)

    async def context_for_run(principal: Any, run_id: str, **_: Any) -> Any:
        return ExecutionContext(
            executive_id="exec-1",
            permissions=frozenset(),
            product_scope=ProductScope(frozenset({"1"}), 1),
            correlation=Correlation(session_id="s", run_id=run_id),
        )

    async def get_run(run_id: str) -> Any:
        return SimpleNamespace(status=RunStatus.RUNNING)

    async def principal(run_id: str) -> object:
        return object()

    return ToolRunner(
        registry=CapabilityRegistry([spec]),
        resolver=cast(Any, SimpleNamespace(context_for_run=context_for_run)),
        principals=cast(Any, SimpleNamespace(get=principal)),
        runs=cast(Any, SimpleNamespace(get_run=get_run)),
        operations=cast(Any, MemoryOperations()),
        budgets=budgets,
        progress=ProgressSink(),
        sleep=sleep,
    )


async def test_a_slow_tool_wait_is_cut_off_at_the_deadline() -> None:
    calls: list[str] = []

    async def slow(args: _In, ctx: OperationContext) -> Any:
        calls.append("started")
        await asyncio.sleep(30)  # a warehouse wait, for example
        return ToolSucceeded(output=_Out(done=True))

    runner = await _runner(_spec(slow, RecoveryMode.NO_RETRY), seconds_left=0.3)
    started = time.monotonic()
    with pytest.raises(RunStopped) as stopped:
        await runner.run("r", "call-1", "slow_tool", {})
    assert stopped.value.resource is BudgetResource.ACTIVE_TIME
    assert time.monotonic() - started < SLACK
    assert calls == ["started"]


async def test_no_retry_starts_once_the_backoff_reaches_the_deadline() -> None:
    calls: list[str] = []

    async def failing(args: _In, ctx: OperationContext) -> Any:
        calls.append("attempt")
        return ToolFailed(code=ToolErrorCode.TEMPORARY_FAILURE, message="try again")

    async def long_backoff(seconds: float) -> None:
        await asyncio.sleep(30)  # e.g. a provider's retry-after hint

    # 1 s left: the first backoff (0.5 s) is allowed, but the wait runs long.
    runner = await _runner(
        _spec(failing, RecoveryMode.RETRY), seconds_left=1.0, sleep=long_backoff
    )
    started = time.monotonic()
    with pytest.raises(RunStopped):
        await runner.run("r", "call-1", "slow_tool", {})
    assert time.monotonic() - started < 1.0 + SLACK
    assert calls == ["attempt"]  # the retry never started


async def test_a_call_after_the_deadline_is_refused_without_running() -> None:
    calls: list[str] = []

    async def handler(args: _In, ctx: OperationContext) -> Any:
        calls.append("ran")
        return ToolSucceeded(output=_Out(done=True))

    runner = await _runner(_spec(handler, RecoveryMode.NO_RETRY), seconds_left=0)
    result = await runner.run("r", "call-1", "slow_tool", {})
    assert isinstance(result.outcome, ToolFailed)
    assert result.outcome.code is ToolErrorCode.BUDGET_EXCEEDED
    assert calls == []
