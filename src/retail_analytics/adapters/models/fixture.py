"""The offline model used when no provider is configured (fixture mode).

It never calls a network service and answers every request with a fixed,
evidence-free notice, so a locally started worker can run an investigation
end to end without credentials. Real providers replace it at the worker's
composition root.
"""

from __future__ import annotations

from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

FIXTURE_ANSWER = (
    "No language model is configured for this backend (fixture mode), so the "
    "question was recorded but not analyzed."
)


async def _respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    answer = next(
        (tool for tool in info.output_tools if "Answer" in tool.name),
        info.output_tools[0] if info.output_tools else None,
    )
    if answer is None:
        raise RuntimeError("the investigation agent declares no output tools")
    return ModelResponse(
        parts=[
            ToolCallPart(
                answer.name,
                {"text": FIXTURE_ANSWER, "cited_evidence": [], "complete": False},
            )
        ]
    )


def fixture_model() -> FunctionModel:
    return FunctionModel(_respond, model_name="fixture")
