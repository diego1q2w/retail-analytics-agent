"""Persistent provider admission before actual requests and uncertain outcomes."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

from retail_analytics.adapters.models.budgeted import BudgetedModel
from retail_analytics.application.budgets import RunBudgets
from retail_analytics.application.investigation_runtime import RunStopped, StopReason
from retail_analytics.domain.budgets import BudgetResource, RunLimits
from tests.unit.budgets.memory_store import MemoryRunBudgetStore

pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 10, 8, tzinfo=UTC)
MESSAGES: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart("Analyze sales.")])]


async def test_provider_limit_survives_reconstructed_wrapper() -> None:
    store = MemoryRunBudgetStore()
    calls = 0

    async def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        return ModelResponse(
            parts=[TextPart("Done.")],
            usage=RequestUsage(input_tokens=10, output_tokens=5),
        )

    provider = FunctionModel(respond)
    limits = RunLimits(provider_requests=1)
    budgets = RunBudgets(store, limits, clock=lambda: NOW)
    first = BudgetedModel(
        provider, budgets, run_id=lambda: "run", request_key=lambda _: "attempt-1"
    )
    await first.request(MESSAGES, None, ModelRequestParameters())
    replacement = BudgetedModel(
        provider,
        RunBudgets(store, limits, clock=lambda: NOW),
        run_id=lambda: "run",
        request_key=lambda _: "attempt-2",
    )
    with pytest.raises(RunStopped) as stopped:
        await replacement.request(MESSAGES, None, ModelRequestParameters())
    assert stopped.value.reason is StopReason.BUDGET
    assert stopped.value.resource is BudgetResource.PROVIDER_REQUESTS
    assert calls == 1
    snapshot = await budgets.snapshot("run")
    assert snapshot and snapshot.usage.tokens == 15


async def test_uncertain_provider_attempt_is_charged_before_retry() -> None:
    store = MemoryRunBudgetStore()
    budgets = RunBudgets(store, RunLimits(), clock=lambda: NOW)

    async def interrupted(
        messages: list[ModelMessage], info: AgentInfo
    ) -> ModelResponse:
        raise RuntimeError("controlled lost provider response")

    provider = FunctionModel(interrupted)
    for attempt in range(2):

        def key(name: str, number: int = attempt) -> str:
            return f"attempt-{number}"

        model = BudgetedModel(
            provider,
            budgets,
            run_id=lambda: "run",
            request_key=key,
        )
        with pytest.raises(RuntimeError, match="controlled"):
            await model.request(MESSAGES, None, ModelRequestParameters())
    snapshot = await budgets.snapshot("run")
    assert snapshot and snapshot.usage.provider_requests == 2
    assert snapshot.usage.tokens > 0
