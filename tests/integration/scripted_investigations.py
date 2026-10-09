"""Controlled tools and model shared by the investigation runtime tests.

Free of Temporal imports: used by the Temporal worker process
(``investigation_worker``) and by the local-manager tests and process.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
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

from retail_analytics.adapters.postgres.database import Database
from retail_analytics.adapters.postgres.investigations import (
    PostgresInvestigationInputs,
)
from retail_analytics.application.contracts.persistence import OperationRequest
from retail_analytics.application.contracts.progress import EventKind
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
from retail_analytics.domain.investigations import InputKind, InputStatus
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
    cited: list[str] = []
    if "use annual sales" in context:
        text = "The updated annual sales investigation is complete."
    if "citation case" in context:
        # Cite the first evidence record the context shows, twice, plus an
        # invented ID that must never become a numbered source.
        shown = re.findall(r"\bevd_[0-9a-f]{32}\b", framed)
        if shown:
            cited = [shown[0]]
            text = (
                f"September sales are in the cited result [{shown[0]}]; the "
                f"same result again [{shown[0]}]; unknown [evd_{'f' * 32}]."
            )
    return ModelResponse(
        parts=[
            ToolCallPart(
                tool.name, {"text": text, "cited_evidence": cited, "complete": True}
            )
        ]
    )


def fallback_chain(settings: BackendSettings) -> Any:
    """Real Gemini/OpenAI adapters over HTTP stubs: Gemini calls the effect
    tool, then is overloaded (503); GPT must continue from that tool result."""
    from tests.unit.models import stubs

    def gemini(request: dict[str, Any]) -> stubs.Reply:
        if any(step["type"] == "function_result" for step in request["input"]) or (
            "user's answer" in json.dumps(request)
        ):
            return stubs.error(503, "unavailable")
        return stubs.Reply(
            events=stubs.gemini_call(
                "checkpoint_effect", {"purpose": "controlled test"}, call_id="g-1"
            )
        )

    def gpt(request: dict[str, Any]) -> stubs.Reply:
        sent = json.dumps(request)
        if "clarification case" in sent and "user's answer" not in sent:
            # Asked once, with a personal-data canary the gate must mask.
            ask = next(t["name"] for t in request["tools"] if "Clarif" in t["name"])
            return stubs.Reply(
                events=stubs.openai_call(
                    ask, {"question": "Which period? (or mail canary@example.com)"}
                )
            )
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


STEERING_TEXT = "Instead use annual sales."
_TERMINAL = {
    EventKind.RUN_COMPLETED,
    EventKind.RUN_PARTIAL,
    EventKind.RUN_FAILED,
    EventKind.RUN_CANCELLED,
}


async def await_terminal_event(db: Persistence, run_id: str) -> None:
    """Until the run's terminal event is published (it follows the close)."""
    async with asyncio.timeout(60):
        while True:
            events = await db.run_events.replay(run_id, limit=10_000)
            if any(e.kind in _TERMINAL for e in events):
                return
            await asyncio.sleep(0.05)


async def steering_outcome(db: Persistence, run_id: str) -> dict[str, Any]:
    """What became of a finished run's steering, from its durable records:
    input statuses, event kinds (in order) and the run's closing message."""
    inputs = await PostgresInvestigationInputs(Database(db.engine)).for_run(run_id)
    events = [e.kind for e in await db.run_events.replay(run_id, limit=10_000)]
    with db.engine.connect() as connection:
        closing = connection.execute(
            sa.text(
                "SELECT content FROM messages WHERE run_id = :run "
                "AND role = 'assistant' ORDER BY position DESC LIMIT 1"
            ),
            {"run": run_id},
        ).scalar_one_or_none()
    steering = [i.status for i in inputs if i.kind is InputKind.STEERING]
    # Never left undecided once the run ended: applied or not applied.
    assert InputStatus.PENDING not in steering
    terminal = [i for i, kind in enumerate(events) if kind in _TERMINAL]
    if EventKind.INPUT_NOT_APPLIED in events:
        assert events.index(EventKind.INPUT_NOT_APPLIED) < terminal[0]
    return {
        "steering": steering,
        "events": events,
        "closing": closing or "",
        "applied_events": events.count(EventKind.INPUT_APPLIED),
    }
