"""Gemini primary / GPT backup through the real adapters and HTTP stubs.

Every provider attempt - retries and fallback included - must be reserved
against the run budget before it is sent, settled with reported usage (or
kept as an estimate when usage is unknown), and the backup must continue
from the same application-owned history without repeating tool effects.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse

from retail_analytics.application.budgets import RetrySettings, RunBudgets
from retail_analytics.application.investigation_runtime import RunStopped, StopReason
from retail_analytics.bootstrap.config import BackendSettings
from retail_analytics.bootstrap.models import provider_chain
from retail_analytics.domain.budgets import (
    BudgetResource,
    Charge,
    ChargeKind,
    RunLimits,
)
from tests.unit.budgets.memory_store import MemoryRunBudgetStore
from tests.unit.models import stubs

pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 10, 9, tzinfo=UTC)
SETTINGS = BackendSettings(retry_base_seconds=0.001, retry_max_seconds=0.01)


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str


@dataclass(frozen=True)
class Deps:
    run_id: str


@dataclass
class Harness:
    store: MemoryRunBudgetStore
    agent: Agent[Deps, Answer]
    tool_calls: list[str]

    async def run(self, question: str = "Sales last month?") -> Any:
        return await self.agent.run(question, deps=Deps("run-1"))

    async def provider_charges(self) -> list[Charge]:
        return [
            c
            for c in await self.store.charges("run-1")
            if c.kind is ChargeKind.PROVIDER_REQUEST
        ]


def harness(
    gemini: stubs.Recorder,
    gpt: stubs.Recorder | None,
    *,
    limits: RunLimits | None = None,
    settings: BackendSettings = SETTINGS,
) -> Harness:
    store = MemoryRunBudgetStore()
    budgets = RunBudgets(
        store,
        limits or RunLimits(),
        retry=RetrySettings(0.001, 0.01),
        clock=lambda: NOW,
        jitter=lambda: 0.0,
    )
    providers = [stubs.gemini(gemini), *([stubs.openai(gpt)] if gpt else [])]
    chain = provider_chain(settings, providers=providers)(budgets)
    agent: Agent[Deps, Answer] = Agent(
        chain, deps_type=Deps, output_type=Answer, instructions="Policy."
    )
    calls: list[str] = []

    @agent.tool_plain
    def execute_analysis(sql: str) -> dict[str, str]:
        """Run an analysis; records one durable effect per call."""
        calls.append(sql)
        return {"evidence_id": "ev-1"}

    return Harness(store, agent, calls)


def gpt_answer(text: str = "Sales were 10.") -> stubs.Reply:
    return stubs.Reply(events=stubs.openai_call("final_result", {"text": text}))


def gemini_answer(text: str = "Sales were 10.") -> stubs.Reply:
    return stubs.Reply(events=stubs.gemini_call("final_result", {"text": text}))


async def test_primary_answers_and_reported_usage_settles_each_request() -> None:
    gemini = stubs.Recorder(
        [
            stubs.Reply(events=stubs.gemini_call("execute_analysis", {"sql": "S1"})),
            gemini_answer(),
        ]
    )
    h = harness(gemini, stubs.Recorder([]))
    result = await h.run()
    assert result.output.text == "Sales were 10."
    assert h.tool_calls == ["S1"]
    charges = await h.provider_charges()
    assert len(charges) == 2
    assert all(c.settled and not c.ambiguous and c.tokens == 125 for c in charges)


async def test_outage_falls_back_with_context_and_without_repeating_tools() -> None:
    gemini = stubs.Recorder(
        [
            stubs.Reply(events=stubs.gemini_call("execute_analysis", {"sql": "S1"})),
            stubs.error(503, "unavailable"),
            stubs.error(503, "unavailable"),
            stubs.error(503, "unavailable"),
        ]
    )
    gpt = stubs.Recorder([gpt_answer()])
    h = harness(gemini, gpt)

    result = await h.run()

    assert result.output.text == "Sales were 10."
    assert h.tool_calls == ["S1"]  # the completed tool effect is not re-run
    sent = gpt.requests[0]["input"]
    assert {"role": "user", "content": "Sales last month?"} in sent
    call = next(i for i in sent if i.get("type") == "function_call")
    output = next(i for i in sent if i.get("type") == "function_call_output")
    assert call["call_id"] == output["call_id"] == "call_1"
    assert json.loads(output["output"]) == {"evidence_id": "ev-1"}
    assert gpt.requests[0]["instructions"] == "Policy."
    assert gpt.requests[0]["store"] is False
    providers = [
        m.provider_name for m in result.all_messages() if isinstance(m, ModelResponse)
    ]
    assert providers == ["google-interactions", "openai"]
    # 1 primary success + 3 failed primary attempts + 1 backup: all counted.
    charges = await h.provider_charges()
    assert len(charges) == 5
    failed = [c for c in charges if c.ambiguous]
    assert len(failed) == 3  # 5xx without usage: the estimate stands
    assert all(c.tokens > 0 for c in failed)


async def test_rate_limit_with_long_retry_hint_falls_back_and_cools_down() -> None:
    gemini = stubs.Recorder(
        [stubs.error(429, "resource_exhausted", details=[{"retryDelay": "40s"}])]
    )
    gpt = stubs.Recorder([gpt_answer("first"), gpt_answer("second")])
    h = harness(gemini, gpt)

    assert (await h.run()).output.text == "first"
    # The cooling primary is skipped: no request, no reservation.
    assert (await h.run()).output.text == "second"

    assert len(gemini.requests) == 1
    charges = await h.provider_charges()
    assert len(charges) == 3
    rejected = charges[0]
    assert rejected.settled and not rejected.ambiguous and rejected.tokens == 0


async def test_short_retry_hint_is_honoured_within_the_primary() -> None:
    gemini = stubs.Recorder(
        [
            stubs.error(429, "resource_exhausted", details=[{"retryDelay": "0s"}]),
            gemini_answer(),
        ]
    )
    h = harness(gemini, stubs.Recorder([]))
    assert (await h.run()).output.text == "Sales were 10."
    assert len(await h.provider_charges()) == 2


async def test_unknown_model_is_not_retried_and_falls_back() -> None:
    gemini = stubs.Recorder([stubs.error(404, "not_found")])
    gpt = stubs.Recorder([gpt_answer()])
    h = harness(gemini, gpt)
    assert (await h.run()).output.text == "Sales were 10."
    assert len(gemini.requests) == 1


async def test_both_providers_failing_stops_the_run_as_unavailable() -> None:
    gemini = stubs.Recorder([stubs.error(503, "unavailable")] * 3)
    gpt = stubs.Recorder([stubs.error(500, "server_error")] * 3)
    h = harness(gemini, gpt)
    with pytest.raises(RunStopped) as stopped:
        await h.run()
    assert stopped.value.reason is StopReason.MODEL_UNAVAILABLE
    assert len(gemini.requests) == 3 and len(gpt.requests) == 3
    assert len(await h.provider_charges()) == 6


async def test_request_budget_stops_retries_and_fallback_before_sending() -> None:
    gemini = stubs.Recorder([stubs.error(503, "unavailable")] * 3)
    gpt = stubs.Recorder([gpt_answer()])
    h = harness(gemini, gpt, limits=RunLimits(provider_requests=2))
    with pytest.raises(RunStopped) as stopped:
        await h.run()
    assert stopped.value.reason is StopReason.BUDGET
    assert stopped.value.resource is BudgetResource.PROVIDER_REQUESTS
    assert len(gemini.requests) == 2
    assert gpt.requests == []  # refused before it was sent


async def test_first_token_timeout_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    from retail_analytics.adapters.models import deadlines

    async def expire(awaitable: Any, seconds: float) -> Any:
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
        raise TimeoutError

    gemini = stubs.Recorder([])
    gpt = stubs.Recorder([gpt_answer()])
    h = harness(gemini, gpt)
    # Only the primary's first-token wait expires.
    primary_deadlines = h.agent.model.wrapped.models[0].wrapped.wrapped  # type: ignore[union-attr]
    assert isinstance(primary_deadlines, deadlines.StreamDeadlines)
    monkeypatch.setattr(primary_deadlines, "_wait_for", expire)

    assert (await h.run()).output.text == "Sales were 10."
    charges = await h.provider_charges()
    assert len(charges) == 4  # three timed-out primary attempts + backup
    assert sum(c.ambiguous for c in charges) == 3


async def test_reasoning_of_one_provider_never_reaches_the_other() -> None:
    gemini = stubs.Recorder(
        [
            stubs.Reply(events=stubs.gemini_call("execute_analysis", {"sql": "S1"})),
            *[stubs.error(503, "unavailable")] * 3,
        ]
    )
    gpt = stubs.Recorder([gpt_answer()])
    h = harness(gemini, gpt)
    await h.run()
    body = json.dumps(gpt.requests[0])
    assert "thought-sig" not in body and "call-sig" not in body
    assert "reasoning" not in json.dumps(gpt.requests[0]["input"])


async def test_secrets_are_not_in_errors_or_charges() -> None:
    gemini = stubs.Recorder([stubs.error(401, "unauthenticated")])
    gpt = stubs.Recorder([stubs.error(401, "invalid_api_key")])
    h = harness(gemini, gpt)
    with pytest.raises(RunStopped) as stopped:
        await h.run()
    chain: list[str] = []
    error: BaseException | None = stopped.value
    while error is not None:
        chain.append(repr(error))
        nested = getattr(error, "exceptions", ())
        chain.extend(repr(e) for e in nested)
        error = error.__cause__
    text = " ".join(chain) + repr(await h.provider_charges())
    assert stubs.GEMINI_KEY not in text and stubs.OPENAI_KEY not in text
