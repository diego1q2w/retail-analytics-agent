"""Focused tools through the real runtime step and the shared guarded model.

``InvestigationRuntime.prepare_model_step`` over the in-memory context world
(the same step the local and Temporal runtimes call): an ordinary request
starts focused; a loader recorded in the run, or steering that needs another
group, broadens the very next step of the same run; instructions always match
the exposed tools; the trace records the initial set and each change.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.tools import ToolDefinition

from retail_analytics.adapters.agent import investigator
from retail_analytics.application.contracts.investigations import FocusReason
from retail_analytics.application.contracts.telemetry import Span
from retail_analytics.application.investigation_policy import (
    LOAD_PREFERENCE_TOOLS,
    LOAD_REPORT_TOOLS,
)
from retail_analytics.application.investigation_runtime import InvestigationRuntime
from retail_analytics.application.telemetry import Telemetry, use_telemetry
from retail_analytics.application.tool_focus import LOADERS, TOOL_GROUPS
from retail_analytics.application.tools import ToolDescriptor
from retail_analytics.domain.executions import ToolExecutionStatus
from retail_analytics.domain.investigations import InputKind
from tests.unit.agent.test_context_eviction import Inputs
from tests.unit.context.support import A, World
from tests.unit.telemetry.recording import RecordingSink

pytestmark = pytest.mark.asyncio

GROUPED = frozenset().union(*(g.tools for g in TOOL_GROUPS))
CORE = frozenset({"execute_analysis", "fetch_evidence", "describe_relation"})
CATALOG = tuple(
    ToolDescriptor(name=name, version=1, description=name, parameters={})
    for name in sorted(CORE | GROUPED | LOADERS)
)


class Operations:
    """The run's recorded tool operations (only loaders matter here)."""

    def __init__(self) -> None:
        self.records: list[Any] = []

    def record(self, capability: str, status: ToolExecutionStatus) -> None:
        self.records.append(SimpleNamespace(capability=capability, status=status))

    async def for_run(self, run_id: str) -> list[Any]:
        return list(self.records)


async def _runtime(
    request: str,
) -> tuple[InvestigationRuntime, Inputs, Operations, str]:
    world = World()
    run_id = world.new_run()
    inputs = Inputs(run_id)
    inputs.add(InputKind.REQUEST, request)
    operations = Operations()
    runtime = InvestigationRuntime(
        runs=cast(Any, world.records),
        principals=cast(Any, SimpleNamespace(get=AsyncMock(return_value=A))),
        inputs=cast(Any, inputs),
        resolver=world.resolver,
        budgets=cast(Any, SimpleNamespace(snapshot=AsyncMock(return_value=None))),
        context=world.builder,
        gate=world.gate,
        registry=cast(Any, SimpleNamespace(catalog=lambda ctx: CATALOG)),
        events=Mock(),
        evidence=world.evidence,
        operations=cast(Any, operations),
        queries=None,
        launcher=Mock(),
    )
    return runtime, inputs, operations, run_id


async def test_ordinary_request_starts_focused_and_loading_broadens_it() -> None:
    runtime, _, operations, run_id = await _runtime("What was revenue in September?")
    first = await runtime.prepare_model_step(run_id)
    assert first.tools == CORE | LOADERS
    assert first.focus is not None and first.focus.active == ()
    assert LOAD_REPORT_TOOLS in first.instructions
    assert "save_report" not in first.instructions

    # A failed loader changes nothing; a successful one broadens the next step.
    operations.record(LOAD_REPORT_TOOLS, ToolExecutionStatus.FAILED)
    assert (await runtime.prepare_model_step(run_id)).tools == first.tools
    operations.record(LOAD_REPORT_TOOLS, ToolExecutionStatus.SUCCEEDED)
    second = await runtime.prepare_model_step(run_id)
    reports = next(g for g in TOOL_GROUPS if g.name == "reports")
    assert reports.tools <= second.tools
    assert LOAD_REPORT_TOOLS not in second.tools
    assert second.focus is not None
    assert ("reports", FocusReason.LOADED) in second.focus.active
    assert "save_report" in second.instructions
    assert LOAD_REPORT_TOOLS not in second.instructions
    # Other groups still wait for their loaders, in any order.
    assert LOAD_PREFERENCE_TOOLS in second.tools


async def test_steering_broadens_the_same_run() -> None:
    runtime, inputs, _, run_id = await _runtime("What was revenue in September?")
    before = await runtime.prepare_model_step(run_id)
    assert "convert_currency" not in before.tools
    inputs.add(InputKind.STEERING, "Show it in euros instead.")
    after = await runtime.prepare_model_step(run_id)
    assert "convert_currency" in after.tools
    assert after.focus is not None
    assert ("currency", FocusReason.REQUEST) in after.focus.active


async def test_trace_records_initial_selection_and_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _, operations, run_id = await _runtime("What was revenue in September?")
    monkeypatch.setattr(
        investigator,
        "current_deps",
        lambda: investigator.InvestigationDeps(run_id=run_id),
    )
    sent: list[set[str]] = []

    async def provider(
        messages: list[ModelMessage], settings: Any, parameters: Any
    ) -> ModelResponse:
        sent.append({t.name for t in parameters.function_tools})
        return ModelResponse(parts=[TextPart("ok")])

    model = investigator.GuardedModel(
        investigator.AgentBinding(
            cast(
                investigator.AgentServices,
                SimpleNamespace(steps=runtime, model=SimpleNamespace(request=provider)),
            )
        )
    )
    offered = ModelRequestParameters(
        function_tools=[
            ToolDefinition(name=d.name, description=d.name) for d in CATALOG
        ]
    )
    conversation: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart("Go.")])]
    sink = RecordingSink()
    with use_telemetry(Telemetry(sink)):
        conversation.append(await model.request(conversation, None, offered))
        conversation.append(await model.request(conversation, None, offered))
        operations.record(LOAD_REPORT_TOOLS, ToolExecutionStatus.SUCCEEDED)
        conversation.append(await model.request(conversation, None, offered))
    focus = [s for s in sink.spans if s.name == Span.TOOL_FOCUS]
    assert [s.attributes["focus.change"] for s in focus] == ["initial", "changed"]
    assert focus[0].attributes["focus.exposed"] == len(CORE | LOADERS)
    assert focus[1].attributes["focus.groups"] == "reports:loaded"
    outputs = focus[1].content("outputs")
    assert isinstance(outputs, dict)
    assert "save_report" in outputs["added"]
    assert outputs["removed"] == [LOAD_REPORT_TOOLS]
    # The provider only ever saw the exposed tools.
    assert sent[0] == set(CORE | LOADERS)
    assert "save_report" in sent[2]
