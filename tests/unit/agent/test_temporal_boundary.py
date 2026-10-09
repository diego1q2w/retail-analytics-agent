"""Typed agent outcomes become Temporal errors only at the Temporal adapter."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, UserPromptPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.function import FunctionModel
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from retail_analytics.adapters.agent import investigator
from retail_analytics.adapters.agent.investigator import AgentBinding, AgentServices
from retail_analytics.adapters.models.budgeted import BudgetedModel
from retail_analytics.adapters.temporal import agent
from retail_analytics.application.contracts.budgets import (
    ProviderPermit,
    ProviderUsage,
)
from retail_analytics.application.contracts.investigations import (
    AgentInterruption,
    InterruptionKind,
    StopReason,
)
from retail_analytics.application.contracts.model_costs import ModelRef
from retail_analytics.application.investigation_runtime import RunStopped
from retail_analytics.domain.budgets import BudgetResource, Charge
from tests.unit.agent.scenario import STEP, FakeSteps, FakeTools, Provider

MESSAGES: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart("Investigate.")])]


class Budget:
    def __init__(self) -> None:
        self.keys: list[str] = []

    async def reserve_provider_request(
        self,
        run_id: str,
        request_key: str,
        *,
        estimated_input_tokens: int,
        model: ModelRef | None = None,
    ) -> ProviderPermit:
        self.keys.append(request_key)
        return ProviderPermit(request_key, estimated_input_tokens, 100_000, 20)

    async def record_provider_usage(
        self,
        run_id: str,
        request_key: str,
        usage: ProviderUsage,
        *,
        model: ModelRef | None = None,
        estimated_input_tokens: int = 0,
    ) -> Charge | None:
        return None


def _temporal_model(
    monkeypatch: pytest.MonkeyPatch, steps: Any, model: Any
) -> agent.TemporalGuardedModel:
    monkeypatch.setattr(
        investigator,
        "current_deps",
        lambda: investigator.InvestigationDeps(run_id="run"),
    )
    return agent.TemporalGuardedModel(
        AgentBinding(AgentServices(steps, FakeTools(), model))
    )


@pytest.mark.asyncio
async def test_run_stopped_becomes_a_non_retryable_temporal_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    steps = FakeSteps(())

    async def spent(run_id: str) -> None:
        raise RunStopped(StopReason.BUDGET, BudgetResource.TOKENS)

    steps.prepare_model_step = spent  # type: ignore[assignment,method-assign]
    model = _temporal_model(monkeypatch, steps, FunctionModel(Provider().respond))
    with pytest.raises(ApplicationError) as caught:
        await model.request(MESSAGES, None, ModelRequestParameters())
    assert caught.value.type == agent.RUN_STOPPED and caught.value.non_retryable
    assert agent.interruption(caught.value) == AgentInterruption(
        InterruptionKind.STOPPED, StopReason.BUDGET, BudgetResource.TOKENS
    )


@pytest.mark.asyncio
async def test_unbound_worker_fails_closed_without_retries() -> None:
    model = agent.TemporalGuardedModel(AgentBinding())
    with pytest.raises(ApplicationError) as caught:
        await model.request(MESSAGES, None, ModelRequestParameters())
    assert caught.value.type == agent.UNBOUND and caught.value.non_retryable


def test_errors_map_to_runtime_neutral_interruptions() -> None:
    wrapped = RuntimeError("activity failed")
    wrapped.__cause__ = ApplicationError("changed", type=agent.CONTEXT_CHANGED)
    assert agent.interruption(wrapped).kind is InterruptionKind.CONTEXT_CHANGED
    cancelled = agent.stopped_error(RunStopped(StopReason.CANCELLED))
    assert agent.interruption(cancelled) == AgentInterruption(
        InterruptionKind.STOPPED, StopReason.CANCELLED
    )
    failed = agent.interruption(RuntimeError("provider down"))
    assert failed == AgentInterruption(InterruptionKind.FAILED)


@pytest.mark.asyncio
async def test_budget_request_key_is_stable_per_model_activity_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    budget = Budget()
    provider = BudgetedModel(
        FunctionModel(Provider().respond, model_name="scripted"),
        budget,
        run_id=lambda: "run",
    )
    model = _temporal_model(monkeypatch, FakeSteps((STEP,)), provider)
    environment = ActivityEnvironment()

    async def request() -> None:
        await model.request(MESSAGES, None, ModelRequestParameters())

    await environment.run(request)
    info = environment.info
    assert budget.keys == [
        f"{info.workflow_id}/{info.activity_id}/{info.attempt}/scripted/0"
    ]
    # Outside an activity every request gets a fresh key.
    await request()
    assert budget.keys[1].startswith("local/")


@pytest.mark.asyncio
async def test_active_deadline_inside_the_model_activity_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow provider call is cut off inside the activity at the run's
    deadline and surfaces as the active-time stop, never as a retry."""
    import asyncio
    from dataclasses import replace

    async def slow(messages: list[ModelMessage], info: Any) -> Any:
        await asyncio.sleep(30)

    steps = FakeSteps((replace(STEP, active_seconds_left=0.2),))
    model = _temporal_model(monkeypatch, steps, FunctionModel(slow))
    with pytest.raises(ApplicationError) as caught:
        await model.request(MESSAGES, None, ModelRequestParameters())
    assert caught.value.type == agent.RUN_STOPPED and caught.value.non_retryable
    assert agent.interruption(caught.value) == AgentInterruption(
        InterruptionKind.STOPPED, StopReason.BUDGET, BudgetResource.ACTIVE_TIME
    )


@pytest.mark.asyncio
async def test_active_deadline_in_a_tool_activity_is_not_retried() -> None:
    from types import SimpleNamespace

    class Deadline:
        async def run(self, *args: Any) -> Any:
            raise RunStopped(StopReason.BUDGET, BudgetResource.ACTIVE_TIME)

    toolset = agent.TemporalCatalogToolset(
        AgentBinding(
            AgentServices(FakeSteps(()), Deadline(), FunctionModel(Provider().respond))
        )
    )
    ctx = SimpleNamespace(
        deps=investigator.InvestigationDeps(run_id="run"), tool_call_id="c1", run_step=1
    )
    with pytest.raises(ApplicationError) as caught:
        await toolset.call_tool("lookup", {}, ctx, None)  # type: ignore[arg-type]
    assert caught.value.type == agent.RUN_STOPPED and caught.value.non_retryable
    assert agent.interruption(caught.value).resource is BudgetResource.ACTIVE_TIME
