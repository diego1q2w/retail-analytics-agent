"""Controlled worker/provider used by process-termination recovery tests."""

from __future__ import annotations

import asyncio
import os
from typing import Any

import sqlalchemy as sa
from pydantic import SecretStr
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    SystemPromptPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from temporalio.client import Client

from retail_analytics.adapters.temporal.scheduler import TemporalInvestigationScheduler
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
from retail_analytics.bootstrap.access import build_access, local_token_authority
from retail_analytics.bootstrap.config import BackendSettings
from retail_analytics.bootstrap.investigations import build_investigations
from retail_analytics.bootstrap.models import provider_chain
from retail_analytics.bootstrap.persistence import Persistence, build_persistence
from retail_analytics.bootstrap.telemetry import install_from_settings
from retail_analytics.bootstrap.temporal import investigation_worker
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
        current = await db.tool_executions.get(ctx.operation_id)
        if current is not None and not current.status.is_terminal:
            await db.tool_executions.transition(
                ctx.operation_id, ToolExecutionStatus.SUCCEEDED, attempt=ctx.attempt
            )
        return ToolSucceeded(output=EffectOutput(reference=ctx.operation_id))

    from datetime import timedelta

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


async def main() -> None:
    settings = BackendSettings(
        database_url=SecretStr(os.environ["T13_DATABASE_URL"]),
        temporal_address=os.environ["T13_TEMPORAL_ADDRESS"],
        temporal_task_queue=os.environ["T13_TASK_QUEUE"],
        auth_signing_key=SecretStr("test-key-" + "x" * 32),
    )
    assert settings.database_url is not None
    if os.environ.get("T30_TRACES_ENDPOINT"):
        install_from_settings(
            settings.model_copy(
                update={
                    "telemetry_enabled": True,
                    "telemetry_traces_endpoint": os.environ["T30_TRACES_ENDPOINT"],
                    "telemetry_metrics_endpoint": os.environ["T30_METRICS_ENDPOINT"],
                    "telemetry_export_timeout_seconds": 1.0,
                    "telemetry_metric_interval_seconds": 1.0,
                }
            ),
            "worker",
        )
    db = build_persistence(settings.database_url.get_secret_value())
    client = await Client.connect(
        settings.temporal_address or "", plugins=[PydanticAIPlugin()]
    )
    scheduler = TemporalInvestigationScheduler(client, settings.temporal_task_queue)
    access = build_access(db, local_token_authority(settings))
    model: Any = FunctionModel(scripted_model, model_name="scripted")
    if os.environ.get("T14_PROVIDERS") == "fallback":
        model = fallback_chain(settings)
    services = build_investigations(
        settings,
        db,
        access,
        scheduler,
        model,
        registry=effect_registry(db),
    )
    try:
        async with investigation_worker(client, settings.temporal_task_queue, services):
            print("WORKER_READY", flush=True)
            await asyncio.Event().wait()
    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
