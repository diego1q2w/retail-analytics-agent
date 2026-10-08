"""The investigation agent: one adaptive Pydantic AI agent made durable by
Temporal.

Pydantic AI runs the agent loop inside the workflow; ``TemporalDurability``
turns every model request and every tool call into a Temporal activity. The
agent is defined at import time (Temporal must register its activities before
any workflow runs), while the application services it needs are bound by the
worker's composition root (``bind_agent_services``) before the worker starts.
Nothing here runs I/O in workflow code.

Inside the activities:

- ``GuardedModel`` (model activity) asks the runtime for the next step under
  current authority - run still running, budget not spent, pending steering
  applied, permission-checked context assembled, tool catalog filtered - puts
  that context in front of the conversation and only then calls the provider
  model. Context (with evidence rows) is rebuilt for every request and never
  stored in workflow history.
- ``CatalogToolset`` (tool activities) lists the executive's
  permission-filtered catalog (``CapabilityRegistry.catalog``) and runs each
  call through ``ToolRunner`` and therefore ``application.tools.invoke``.
  Tool results returned to the model are compact contracts (evidence IDs, no
  rows).

A ``RunStopped`` raised by the runtime becomes a non-retryable activity
failure the workflow recognises by type and handles (partial findings or
cancellation), never a generic retry.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue
from pydantic_ai import Agent, RunContext
from pydantic_ai._run_context import get_current_run_context
from pydantic_ai.durable_exec.temporal import TemporalDurability
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
)
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.profiles import DEFAULT_PROFILE, ModelProfile
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import AbstractToolset, ToolsetTool
from pydantic_ai.toolsets._dynamic import DynamicToolset
from pydantic_core import SchemaValidator, core_schema
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

from retail_analytics.application.investigation_runtime import (
    ModelStep,
    RunStopped,
)
from retail_analytics.application.tools import ToolDescriptor, ToolResult

AGENT_NAME = "investigator"
TOOLSET_ID = "catalog"
RUN_STOPPED = "RunStopped"
CONTEXT_CHANGED = "InvestigationContextChanged"
_HISTORY_PROVENANCE = "retail_context"
MAX_ANSWER_CHARS = 20_000

# Activity limits. A worker that dies is detected by the heartbeat timeout and
# the activity is retried on another worker; retries are bounded and every
# retry re-checks authority and budgets, and reconciles external effects.
MODEL_ACTIVITY_TIMEOUT = timedelta(minutes=5)
MODEL_HEARTBEAT_TIMEOUT = timedelta(seconds=30)
TOOL_ACTIVITY_TIMEOUT = timedelta(minutes=10)
TOOL_HEARTBEAT_TIMEOUT = timedelta(seconds=15)
ACTIVITY_ATTEMPTS = 5


class InvestigationDeps(BaseModel):
    """What the agent run carries through workflow history: identifiers only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(min_length=1, max_length=128)


class AnswerOutput(BaseModel):
    """The final answer, released only through the output privacy gate."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(
        min_length=1,
        max_length=MAX_ANSWER_CHARS,
        description="The answer: findings, limitations and suggested actions.",
    )
    cited_evidence: list[str] = Field(
        default_factory=list,
        max_length=50,
        description="Evidence ids that support the figures in the answer.",
    )
    complete: bool = Field(
        default=True,
        description="False when part of the question could not be answered.",
    )


class ClarificationOutput(BaseModel):
    """One focused question; the investigation waits for the user's reply."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=1000)


# Services bound at worker start


class ModelSteps(Protocol):
    async def prepare_model_step(self, run_id: str) -> ModelStep: ...

    async def catalog(self, run_id: str) -> tuple[ToolDescriptor, ...]: ...


class ToolCalls(Protocol):
    async def run(
        self,
        run_id: str,
        tool_call_id: str,
        name: str,
        arguments: dict[str, JsonValue],
    ) -> ToolResult[Any]: ...


@dataclass(frozen=True)
class AgentServices:
    steps: ModelSteps
    tools: ToolCalls
    # The provider model (or fallback chain), already wrapped in budget
    # accounting (``adapters.models.budgeted.BudgetedModel``).
    model: Model


_services: AgentServices | None = None


def bind_agent_services(services: AgentServices) -> None:
    """Called once by a worker's composition root before it starts polling."""
    global _services
    _services = services


def _bound() -> AgentServices:
    if _services is None:
        # Fail closed: an unbound worker must not run model or tool work.
        raise ApplicationError(
            "investigation services are not bound", type="Unbound", non_retryable=True
        )
    return _services


def _deps() -> InvestigationDeps:
    context = get_current_run_context()
    deps = getattr(context, "deps", None)
    if not isinstance(deps, InvestigationDeps):
        raise ApplicationError(
            "missing investigation deps", type="Unbound", non_retryable=True
        )
    return deps


def stopped_error(stopped: RunStopped) -> ApplicationError:
    resource = None if stopped.resource is None else stopped.resource.value
    return ApplicationError(
        "investigation stopped",
        stopped.reason.value,
        resource,
        type=RUN_STOPPED,
        non_retryable=True,
    )


# Model


class GuardedModel(Model):
    """Runs inside the model activity: fresh authority and context first."""

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        services = _bound()
        deps = _deps()
        try:
            step = await services.steps.prepare_model_step(deps.run_id)
            provenance = {
                "key": step.history_key,
                "evidence": dict(step.evidence_versions),
            }
            for message in messages:
                if not isinstance(message, ModelResponse):
                    continue
                previous = (message.metadata or {}).get(_HISTORY_PROVENANCE)
                if (
                    not isinstance(previous, dict)
                    or previous.get("key") != step.history_key
                    or not isinstance(previous.get("evidence"), dict)
                    or any(
                        dict(step.evidence_versions).get(key) != version
                        for key, version in previous["evidence"].items()
                    )
                ):
                    # Restart the agent loop; never replay derived claims or
                    # tool arguments from a context that is no longer valid.
                    raise ApplicationError(
                        "investigation context changed",
                        type=CONTEXT_CHANGED,
                        non_retryable=True,
                    )
            parameters = replace(
                model_request_parameters,
                function_tools=[
                    tool
                    for tool in model_request_parameters.function_tools
                    if tool.name in step.tools
                ],
            )
            framed: list[ModelMessage] = [
                ModelRequest(parts=[SystemPromptPart(step.instructions)]),
                *messages,
            ]
            response = await services.model.request(framed, model_settings, parameters)
            return replace(
                response,
                metadata={**(response.metadata or {}), _HISTORY_PROVENANCE: provenance},
            )
        except RunStopped as stopped:
            raise stopped_error(stopped) from None

    @property
    def model_name(self) -> str:
        return AGENT_NAME

    @property
    def system(self) -> str:
        return "retail-analytics"

    @property
    def profile(self) -> ModelProfile:
        # The bound provider's profile shapes tool schemas and output mode.
        if _services is not None:
            return _services.model.profile
        return DEFAULT_PROFILE


# Tools

_ANY_ARGUMENTS = SchemaValidator(schema=core_schema.any_schema())


class CatalogToolset(AbstractToolset[InvestigationDeps]):
    """The executive's permission-filtered catalog; built inside activities.

    Arguments are not validated here: the single tool path validates them
    strictly and reports problems as an ``INVALID_INPUT`` result.
    """

    @property
    def id(self) -> str:
        return TOOLSET_ID

    async def get_tools(
        self, ctx: RunContext[InvestigationDeps]
    ) -> dict[str, ToolsetTool[InvestigationDeps]]:
        descriptors = await _bound().steps.catalog(ctx.deps.run_id)
        return {
            d.name: self._tool(
                ToolDefinition(
                    name=d.name,
                    description=d.description,
                    parameters_json_schema=dict(d.parameters),
                )
            )
            for d in descriptors
        }

    async def get_tool_for_tool_def(
        self, tool_def: ToolDefinition, ctx: RunContext[InvestigationDeps]
    ) -> ToolsetTool[InvestigationDeps]:
        # Never re-list: a tool revoked since listing still reaches the gate,
        # which refuses it as unavailable.
        return self._tool(tool_def)

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[InvestigationDeps],
        tool: ToolsetTool[InvestigationDeps],
    ) -> dict[str, JsonValue]:
        call_id = ctx.tool_call_id or f"{name}-{ctx.run_step}"
        result = await _bound().tools.run(
            ctx.deps.run_id, call_id, name, dict(tool_args)
        )
        compact: dict[str, JsonValue] = result.model_dump(
            mode="json", exclude_none=True
        )
        return compact

    def _tool(self, definition: ToolDefinition) -> ToolsetTool[InvestigationDeps]:
        return ToolsetTool(
            toolset=self,
            tool_def=definition,
            max_retries=0,
            args_validator=_ANY_ARGUMENTS,
        )


def _catalog(ctx: RunContext[InvestigationDeps]) -> AbstractToolset[InvestigationDeps]:
    return CatalogToolset()


def _retry(attempts: int) -> RetryPolicy:
    return RetryPolicy(
        initial_interval=timedelta(seconds=1),
        maximum_interval=timedelta(seconds=20),
        maximum_attempts=attempts,
        non_retryable_error_types=[RUN_STOPPED, "Unbound"],
    )


investigation_agent: Agent[InvestigationDeps, AnswerOutput | ClarificationOutput] = (
    Agent(
        GuardedModel(),
        name=AGENT_NAME,
        deps_type=InvestigationDeps,
        output_type=[AnswerOutput, ClarificationOutput],
        toolsets=[DynamicToolset(_catalog, id=TOOLSET_ID)],
        retries=1,
        capabilities=[
            TemporalDurability(
                activity_config={
                    "start_to_close_timeout": TOOL_ACTIVITY_TIMEOUT,
                    "retry_policy": _retry(ACTIVITY_ATTEMPTS),
                },
                model_activity_config={
                    "start_to_close_timeout": MODEL_ACTIVITY_TIMEOUT,
                    "heartbeat_timeout": MODEL_HEARTBEAT_TIMEOUT,
                    "retry_policy": _retry(3),
                },
                toolset_activity_config={
                    TOOLSET_ID: {
                        "start_to_close_timeout": TOOL_ACTIVITY_TIMEOUT,
                        "heartbeat_timeout": TOOL_HEARTBEAT_TIMEOUT,
                        "retry_policy": _retry(ACTIVITY_ATTEMPTS),
                    }
                },
            )
        ],
    )
)


def is_run_stopped(error: BaseException) -> tuple[str, str | None] | None:
    """(reason, resource) when ``error`` (or a cause) is a ``RunStopped``."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ApplicationError) and current.type == RUN_STOPPED:
            details: Sequence[Any] = current.details
            reason = str(details[0]) if details else "interrupted"
            resource = details[1] if len(details) > 1 else None
            return reason, None if resource is None else str(resource)
        cause = getattr(current, "cause", None)
        current = cause if isinstance(cause, BaseException) else current.__cause__
    return None


def is_context_changed(error: BaseException) -> bool:
    """Recognize a context restart through Temporal and agent error wrappers."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ApplicationError) and current.type == CONTEXT_CHANGED:
            return True
        cause = getattr(current, "cause", None)
        current = cause if isinstance(cause, BaseException) else current.__cause__
    return False
