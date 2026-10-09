"""Controlled tools and model shared by the investigation runtime tests.

Free of Temporal imports: used by the Temporal worker process
(``investigation_worker``) and by the local-manager tests and process.
"""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from typing import Any

import sqlalchemy as sa
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    SystemPromptPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo

from retail_analytics.application.contracts.persistence import OperationRequest
from retail_analytics.application.tools import (
    AuthorizationSpec,
    CapabilityRegistry,
    CapabilitySpec,
    OperationContext,
    RetrySpec,
    ToolInput,
    ToolOutput,
    ToolSucceeded,
)
from retail_analytics.bootstrap.config import BackendSettings
from retail_analytics.bootstrap.models import provider_chain
from retail_analytics.bootstrap.persistence import Persistence
from retail_analytics.domain.executions import ToolExecutionStatus
from retail_analytics.domain.operations import RecoveryMode, SideEffect


class EffectInput(ToolInput):
    purpose: str


class EffectOutput(ToolOutput):
    reference: str


def effect_registry(db: Persistence) -> CapabilityRegistry:
    async def effect(
        args: EffectInput, ctx: OperationContext
    ) -> ToolSucceeded[EffectOutput]:
        await db.tool_executions.begin(
            OperationRequest(
                operation_id=ctx.operation_id,
                run_id=ctx.execution.correlation.run_id,
                capability="checkpoint_effect",
                capability_version=1,
                side_effect=SideEffect.EXTERNAL_JOB,
            )
        )

        def commit_effect() -> bool:
            with db.engine.begin() as connection:
                result = connection.execute(
                    sa.text(
                        "INSERT INTO t13_test_effects (operation_id, run_id) "
                        "VALUES (:op, :run) "
                        "ON CONFLICT (operation_id) DO NOTHING"
                    ),
                    {"op": ctx.operation_id, "run": ctx.execution.correlation.run_id},
                )
                return result.rowcount == 1

        created = await asyncio.to_thread(commit_effect)
        if created and os.environ.get("T13_CRASH_HOLD") == "1":
            await asyncio.sleep(120)
        if created:
            # Lets a test act (steer, shut down) while the effect is running.
            await asyncio.sleep(float(os.environ.get("T13_EFFECT_DELAY", "0")))
        current = await db.tool_executions.get(ctx.operation_id)
        if current is not None and not current.status.is_terminal:
            await db.tool_executions.transition(
                ctx.operation_id, ToolExecutionStatus.SUCCEEDED, attempt=ctx.attempt
            )
        return ToolSucceeded(output=EffectOutput(reference=ctx.operation_id))

    return CapabilityRegistry(
        [
            CapabilitySpec(
                name="checkpoint_effect",
                version=1,
                description="Record a controlled durable effect.",
                progress_label="Recording controlled effect.",
                input_model=EffectInput,
                output_model=EffectOutput,
                handler=effect,
                authorization=AuthorizationSpec(
                    required_permissions=frozenset({"analysis:read"})
                ),
                side_effect=SideEffect.EXTERNAL_JOB,
                retry=RetrySpec(RecoveryMode.RECONCILE_FIRST, 3, timedelta(minutes=4)),
            )
        ]
    )


async def scripted_model(
    messages: list[ModelMessage], info: AgentInfo
) -> ModelResponse:
    framed = "\n".join(
        part.content
        for message in messages
        for part in message.parts
        if isinstance(part, SystemPromptPart)
    )
    context = framed.rsplit("<request>", 1)[-1].split("</request>", 1)[0]
    if "effect case" in context and not any(
        isinstance(part, ToolReturnPart)
        for message in messages
        for part in message.parts
    ):
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "checkpoint_effect",
                    {"purpose": "controlled test"},
                    tool_call_id="stable-effect",
                )
            ]
        )
    if "clarification case" in context and "user's answer" not in context:
        tool = next(tool for tool in info.output_tools if "Clarification" in tool.name)
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool.name, {"question": "Which sales period should I use?"}
                )
            ]
        )
    if "slow case" in context:
        await asyncio.sleep(3)
    tool = next(tool for tool in info.output_tools if "Answer" in tool.name)
    text = "The requested sales investigation is complete."
    if "use annual sales" in context:
        text = "The updated annual sales investigation is complete."
    return ModelResponse(
        parts=[
            ToolCallPart(
                tool.name, {"text": text, "cited_evidence": [], "complete": True}
            )
        ]
    )


def fallback_chain(settings: BackendSettings) -> Any:
    """Real Gemini/OpenAI adapters over HTTP stubs: Gemini calls the effect
    tool, then is overloaded (503); GPT must continue from that tool result."""
    from tests.unit.models import stubs

    def gemini(request: dict[str, Any]) -> stubs.Reply:
        if any(step["type"] == "function_result" for step in request["input"]):
            return stubs.error(503, "unavailable")
        return stubs.Reply(
            events=stubs.gemini_call(
                "checkpoint_effect", {"purpose": "controlled test"}, call_id="g-1"
            )
        )

    def gpt(request: dict[str, Any]) -> stubs.Reply:
        answer = next(t["name"] for t in request["tools"] if "Answer" in t["name"])
        seen = [
            item["call_id"]
            for item in request["input"]
            if item.get("type") == "function_call_output"
        ]
        text = f"Backup continued after {','.join(seen) or 'nothing'}."
        return stubs.Reply(
            events=stubs.openai_call(
                answer, {"text": text, "cited_evidence": [], "complete": True}
            )
        )

    return provider_chain(
        settings,
        providers=[
            stubs.gemini(stubs.Recorder([], script=gemini)),
            stubs.openai(stubs.Recorder([], script=gpt)),
        ],
    )
