"""Context eviction is not evidence invalidation (real runtime step + guard).

The real ``InvestigationRuntime.prepare_model_step`` and ``ContextBuilder``
over the in-memory context world, the shared ``GuardedModel`` (local and
Temporal runtimes use the same one) and a scripted provider. Four earlier
session records plus four new query results cross the six-record prompt
bound; the conversation (and its discovery work) must survive that, while
real invalidation, lost scope, unknown validity, steering, preference
changes and topic resets still restart it with a sanitized cause code.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters

from retail_analytics.adapters.agent import investigator
from retail_analytics.application.contracts.investigations import (
    ContextRestartCause,
)
from retail_analytics.application.contracts.telemetry import Label, Metric, Span
from retail_analytics.application.investigation_runtime import (
    InvestigationContextChanged,
    InvestigationRuntime,
)
from retail_analytics.application.telemetry import Telemetry, use_telemetry
from retail_analytics.domain.context import ContextBudget
from retail_analytics.domain.investigations import InputKind, InputStatus, RunInput
from retail_analytics.domain.preferences import PreferenceKind, PreferenceSetting
from tests.unit.context.support import NARROW, A, World
from tests.unit.privacy.support import EXEC_A
from tests.unit.telemetry.recording import RecordingSink

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 10, 8, tzinfo=UTC)
PARAMETERS = ModelRequestParameters()


@dataclass
class Inputs:
    """The run's persisted inputs: the request, then any applied steering."""

    run_id: str
    items: list[RunInput] = field(default_factory=list)

    def add(self, kind: InputKind, content: str) -> None:
        self.items.append(
            RunInput(
                f"in{len(self.items)}",
                "s-a",
                kind,
                content,
                InputStatus.APPLIED,
                NOW,
                run_id=self.run_id,
            )
        )

    async def apply_pending(self, run_id: str) -> None:
        return None

    async def for_run(self, run_id: str) -> list[RunInput]:
        return list(self.items)


@dataclass
class Investigation:
    world: World
    run_id: str
    inputs: Inputs
    runtime: InvestigationRuntime
    model: investigator.GuardedModel
    provider: Any
    conversation: list[ModelMessage]
    queries: int = 0

    async def step(self) -> ModelResponse:
        """One model request; the scripted model then runs one query."""
        response = await self.model.request(self.conversation, None, PARAMETERS)
        call = ToolCallPart("execute_analysis", {"n": self.queries}, f"c{self.queries}")
        self.conversation += [
            replace(response, parts=[call]),
            ModelRequest(
                parts=[ToolReturnPart(call.tool_name, "ok", call.tool_call_id)]
            ),
        ]
        return response

    async def query(self) -> None:
        self.queries += 1
        await self.world.query(self.run_id, values={"min_amount": 100 + self.queries})
        self.world.tick()


async def _investigation(
    monkeypatch: pytest.MonkeyPatch, budget: ContextBudget | None = None
) -> Investigation:
    w = World(budget=budget)
    earlier = w.new_run()
    # An earlier data overview in the same session left four records.
    for i in range(4):
        await w.query(earlier, values={"min_amount": i + 1})
        w.tick()
    run_id = w.new_run()
    inputs = Inputs(run_id)
    inputs.add(InputKind.REQUEST, "What was the revenue in September?")
    runtime = InvestigationRuntime(
        runs=cast(Any, w.records),
        principals=cast(Any, SimpleNamespace(get=AsyncMock(return_value=A))),
        inputs=cast(Any, inputs),
        resolver=w.resolver,
        budgets=cast(Any, SimpleNamespace(snapshot=AsyncMock(return_value=None))),
        context=w.builder,
        gate=w.gate,
        registry=cast(Any, SimpleNamespace(catalog=lambda ctx: ())),
        events=Mock(),
        evidence=w.evidence,
        operations=Mock(for_run=AsyncMock(return_value=())),
        queries=None,
        launcher=Mock(),
    )
    monkeypatch.setattr(
        investigator,
        "current_deps",
        lambda: investigator.InvestigationDeps(run_id=run_id),
    )
    provider = SimpleNamespace(
        request=AsyncMock(
            side_effect=lambda *_: ModelResponse(
                parts=[ToolCallPart("list_relations", {}, "discover")]
            )
        )
    )
    services = cast(
        investigator.AgentServices, SimpleNamespace(steps=runtime, model=provider)
    )
    model = investigator.GuardedModel(investigator.AgentBinding(services))
    conversation: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart("Investigate.")])
    ]
    return Investigation(w, run_id, inputs, runtime, model, provider, conversation)


async def _past_the_bound(inv: Investigation) -> str:
    """Four queries after the first step; returns the first evicted record."""
    first = await inv.runtime.prepare_model_step(inv.run_id)
    await inv.step()
    for _ in range(4):
        await inv.query()
        await inv.step()
    now = await inv.runtime.prepare_model_step(inv.run_id)
    evicted = {e for e, _ in first.evidence_versions} - {
        e for e, _ in now.evidence_versions
    }
    assert evicted, "the scenario must push earlier evidence out of the prompt"
    return sorted(evicted)[0]


@pytest.mark.parametrize(
    "budget",
    [None, ContextBudget(max_tokens=800, max_rows_per_evidence=2)],
    ids=["count-limit", "size-limit"],
)
async def test_valid_work_survives_the_prompt_bound(
    monkeypatch: pytest.MonkeyPatch, budget: ContextBudget | None
) -> None:
    inv = await _investigation(monkeypatch, budget)
    sink = RecordingSink()
    with use_telemetry(Telemetry(sink)):
        await _past_the_bound(inv)
        step = await inv.runtime.prepare_model_step(inv.run_id)
        # The old rule (only what this prompt shows is valid) restarts here.
        assert (
            investigator.restart_cause(inv.conversation, replace(step, standing=None))
            is ContextRestartCause.EVIDENCE_INVALIDATED
        )
        assert investigator.restart_cause(inv.conversation, step) is None
        await inv.model.request(inv.conversation, None, PARAMETERS)
    # Every request reached the provider with the whole conversation,
    # including the first discovery step: nothing was rediscovered.
    assert inv.provider.request.await_count == 6
    last = inv.provider.request.await_args.args[0]
    assert sum(isinstance(m, ModelResponse) for m in last) == 5
    assert not [s for s in sink.spans if s.name == Span.CONTEXT_RESTART]
    assert not [c for c in sink.counts if c[0] is Metric.CONTEXT_RESTARTS]


async def _restarted(inv: Investigation) -> ContextRestartCause:
    sink = RecordingSink()
    with (
        use_telemetry(Telemetry(sink)),
        pytest.raises(InvestigationContextChanged) as raised,
    ):
        await inv.model.request(inv.conversation, None, PARAMETERS)
    cause = raised.value.cause
    assert sink.counts == [(Metric.CONTEXT_RESTARTS, 1.0, {Label.REASON: cause.value})]
    (span,) = sink.spans
    assert span.name == Span.CONTEXT_RESTART and span.run_id == inv.run_id
    assert span.attributes == {"restart.cause": cause.value}
    # The trace shows the cause as content too (T30-F2), nothing else.
    (captured,) = span.payloads
    assert isinstance(captured.content, dict)
    assert captured.content["restart_cause"] == cause.value
    return cause


async def test_omitted_record_that_is_invalidated_still_restarts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inv = await _investigation(monkeypatch)
    evicted = await _past_the_bound(inv)
    inv.world.store.invalidated.add(evicted)
    calls = inv.provider.request.await_count
    assert await _restarted(inv) is ContextRestartCause.EVIDENCE_INVALIDATED
    assert inv.provider.request.await_count == calls


async def test_omitted_record_with_unknown_validity_restarts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inv = await _investigation(monkeypatch)
    evicted = await _past_the_bound(inv)
    del inv.world.store.records[evicted]
    assert await _restarted(inv) is ContextRestartCause.EVIDENCE_INVALIDATED


async def test_changed_definition_restarts(monkeypatch: pytest.MonkeyPatch) -> None:
    inv = await _investigation(monkeypatch)
    await _past_the_bound(inv)
    await inv.world.store.invalidate_dependent_findings(
        EXEC_A, "s-a", "metric_definition:revenue"
    )
    assert await _restarted(inv) is ContextRestartCause.EVIDENCE_INVALIDATED


async def test_lost_scope_restarts(monkeypatch: pytest.MonkeyPatch) -> None:
    inv = await _investigation(monkeypatch)
    await _past_the_bound(inv)
    inv.world.set_products(EXEC_A, NARROW)
    assert await _restarted(inv) is ContextRestartCause.AUTHORITY_CHANGED


async def test_steering_restarts(monkeypatch: pytest.MonkeyPatch) -> None:
    inv = await _investigation(monkeypatch)
    await _past_the_bound(inv)
    inv.inputs.add(InputKind.STEERING, "Use net revenue instead.")
    assert await _restarted(inv) is ContextRestartCause.REQUEST_CHANGED


async def test_preference_change_restarts(monkeypatch: pytest.MonkeyPatch) -> None:
    inv = await _investigation(monkeypatch)
    await _past_the_bound(inv)
    await inv.world.preferences.remember(
        A, PreferenceSetting(PreferenceKind.DISPLAY_CURRENCY, "EUR")
    )
    assert await _restarted(inv) is ContextRestartCause.PREFERENCES_CHANGED


async def test_topic_reset_restarts(monkeypatch: pytest.MonkeyPatch) -> None:
    inv = await _investigation(monkeypatch)
    await _past_the_bound(inv)
    inv.world.tick()
    await inv.world.builder.reset_topic(A, "s-a", "reset-1")
    assert await _restarted(inv) is ContextRestartCause.TOPIC_RESET
