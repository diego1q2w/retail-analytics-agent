"""Controlled services for the shared agent: no Temporal, database or provider.

Also imported by the import-guard subprocess (``test_without_temporal``), so
it must not import Temporal or pytest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import JsonValue
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    SystemPromptPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from retail_analytics.adapters.agent.investigator import (
    AgentBinding,
    AgentServices,
    InvestigationAgent,
    build_investigation_agent,
)
from retail_analytics.application.contracts.investigations import ModelStep
from retail_analytics.application.tools import (
    ToolDescriptor,
    ToolOutput,
    ToolResult,
    ToolSucceeded,
)

STEP = ModelStep(
    "Answer from the permitted evidence.",
    frozenset({"lookup"}),
    "scope-v1",
    (("e1", 1),),
)
_PARAMETERS: dict[str, JsonValue] = {
    "type": "object",
    "properties": {"metric": {"type": "string"}},
}


class LookupOutput(ToolOutput):
    reference: str


@dataclass
class FakeSteps:
    """``InvestigationRuntime`` steps: one prepared step per model request."""

    steps: tuple[ModelStep, ...]
    calls: int = 0

    async def prepare_model_step(self, run_id: str) -> ModelStep:
        self.calls += 1
        return self.steps[min(self.calls, len(self.steps)) - 1]

    async def catalog(self, run_id: str) -> tuple[ToolDescriptor, ...]:
        # ``revoked`` is listed but not permitted by the prepared step.
        return tuple(
            ToolDescriptor(
                name=name, version=1, description=f"{name} tool", parameters=_PARAMETERS
            )
            for name in ("lookup", "revoked")
        )


@dataclass
class FakeTools:
    """The single tool path (``ToolRunner``): records calls, returns evidence."""

    calls: list[tuple[str, str, str, dict[str, JsonValue]]] = field(
        default_factory=list
    )

    async def run(
        self,
        run_id: str,
        tool_call_id: str,
        name: str,
        arguments: dict[str, JsonValue],
    ) -> ToolResult[Any]:
        self.calls.append((run_id, tool_call_id, name, arguments))
        return ToolResult(
            call_id=tool_call_id,
            capability=name,
            capability_version=1,
            operation_id="op-" + tool_call_id,
            outcome=ToolSucceeded(output=LookupOutput(reference="e1")),
        )


@dataclass
class Provider:
    """A controlled provider: looks a figure up, then answers or asks."""

    ends_with: str = "answer"
    requests: list[list[ModelMessage]] = field(default_factory=list)
    offered_tools: list[set[str]] = field(default_factory=list)

    async def respond(
        self, messages: list[ModelMessage], info: AgentInfo
    ) -> ModelResponse:
        self.requests.append(messages)
        self.offered_tools.append({tool.name for tool in info.function_tools})
        if not any(
            isinstance(part, ToolReturnPart)
            for message in messages
            for part in message.parts
        ):
            return ModelResponse(
                parts=[ToolCallPart("lookup", {"metric": "sales"}, tool_call_id="c1")]
            )
        wanted = "Clarification" if self.ends_with == "clarify" else "Answer"
        output = next(tool for tool in info.output_tools if wanted in tool.name)
        arguments: dict[str, Any] = (
            {"question": "Which sales period should I use?"}
            if self.ends_with == "clarify"
            else {"text": "Sales grew 4%.", "cited_evidence": ["e1"], "complete": True}
        )
        return ModelResponse(parts=[ToolCallPart(output.name, arguments)])

    def instructions(self, request: int) -> list[str]:
        return [
            part.content
            for message in self.requests[request]
            for part in message.parts
            if isinstance(part, SystemPromptPart)
        ]


@dataclass
class Scenario:
    agent: InvestigationAgent
    steps: FakeSteps
    tools: FakeTools
    provider: Provider


def scenario(*steps: ModelStep, ends_with: str = "answer") -> Scenario:
    """The shared agent over controlled services (default: one valid step)."""
    fake_steps = FakeSteps(steps or (STEP,))
    tools = FakeTools()
    provider = Provider(ends_with)
    services = AgentServices(
        fake_steps, tools, FunctionModel(provider.respond, model_name="scripted")
    )
    return Scenario(
        build_investigation_agent(AgentBinding(services)), fake_steps, tools, provider
    )
