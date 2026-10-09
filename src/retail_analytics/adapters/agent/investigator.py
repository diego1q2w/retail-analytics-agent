"""The adaptive investigation agent: one Pydantic AI agent for every runtime.

``build_investigation_agent`` constructs it over an ``AgentBinding`` - the
application services it uses (``InvestigationRuntime`` steps, the
``ToolRunner`` and the budgeted provider model) - so no service is owned by a
module import. A runtime makes the agent durable by adding its capabilities
and translates the typed outcomes raised here into its own errors.

- ``GuardedModel`` asks the runtime for the next step under current authority
  (run still running, budget not spent, pending steering applied,
  permission-checked context assembled, tool catalog filtered), refuses a
  conversation whose source context is no longer valid
  (``InvestigationContextChanged``), puts the context in front of the
  conversation and only then calls the provider model. Context (with
  evidence rows) is rebuilt for every request and never stored in the
  conversation.
- ``CatalogToolset`` lists the executive's permission-filtered catalog
  (``CapabilityRegistry.catalog``) and runs each call through ``ToolRunner``
  and therefore ``application.tools.invoke``. Tool results returned to the
  model are compact contracts (evidence IDs, no rows).
- ``proposal`` maps the agent's typed output to the draft the runtime
  releases (``InvestigationRuntime.release_answer``/``ask``).

``RunStopped`` (from the runtime or budget accounting) and
``InvestigationContextChanged`` are raised as they are; ``AgentUnbound``
fails closed when no services are bound.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue
from pydantic_ai import Agent, RunContext
from pydantic_ai._run_context import get_current_run_context
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
)
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.profiles import DEFAULT_PROFILE, ModelProfile
from pydantic_ai.run import AgentRunResult
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import AbstractToolset, ToolsetTool
from pydantic_ai.toolsets._dynamic import DynamicToolset
from pydantic_ai.usage import UsageLimits
from pydantic_core import SchemaValidator, core_schema

from retail_analytics.application.contracts.investigations import (
    AnswerDraft,
    ContextKeyPart,
    ContextRestartCause,
    ModelStep,
    QuestionDraft,
)
from retail_analytics.application.contracts.telemetry import Label, Metric, Span
from retail_analytics.application.investigation_runtime import (
    InvestigationContextChanged,
)
from retail_analytics.application.telemetry import (
    ATTRIBUTION_METADATA_KEY,
    attribution_from_metadata,
    telemetry,
)
from retail_analytics.application.tools import ToolDescriptor, ToolResult

# Durable runtimes derive identifiers from these names (Temporal activity
# names, for example); renaming them breaks replay of running investigations.
AGENT_NAME = "investigator"
TOOLSET_ID = "catalog"
MAX_ANSWER_CHARS = 20_000
_HISTORY_PROVENANCE = "retail_context"
# The request itself is persisted; the model sees it in the rebuilt context.
AGENT_PROMPT = "Investigate the persisted request supplied by the activity context."
AGENT_REQUEST_LIMIT = 25
AGENT_TOOL_CALLS_LIMIT = 100


class InvestigationDeps(BaseModel):
    """What the agent run carries (and a durable runtime records): IDs only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(min_length=1, max_length=128)


class AnswerOutput(BaseModel):
    """The final answer, released only through the output privacy gate."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(
        min_length=1,
        max_length=MAX_ANSWER_CHARS,
        description=(
            "The answer, shaped like the request: for a figure question the "
            "figure with its period, definition and evidence id in a few "
            "sentences; findings, limitations and suggested actions only for "
            "investigations and reports."
        ),
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


type InvestigationOutput = AnswerOutput | ClarificationOutput
type InvestigationAgent = Agent[InvestigationDeps, InvestigationOutput]


# Services the agent uses (implemented by the application)


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


class AgentUnbound(Exception):
    """No services (or no run deps): model and tool work must not run."""


class AgentBinding:
    """The services an agent instance uses, supplied by a composition root.

    A runtime that constructs its agent before its services exist (Temporal
    registers activities at import) binds them later; others pass them in.
    """

    def __init__(self, services: AgentServices | None = None) -> None:
        self._services = services

    def bind(self, services: AgentServices) -> None:
        self._services = services

    @property
    def services(self) -> AgentServices | None:
        return self._services

    def require(self) -> AgentServices:
        if self._services is None:
            # Fail closed: an unbound agent must not run model or tool work.
            raise AgentUnbound("investigation services are not bound")
        return self._services


def current_deps() -> InvestigationDeps:
    """The deps of the agent run in progress."""
    deps = getattr(get_current_run_context(), "deps", None)
    if not isinstance(deps, InvestigationDeps):
        raise AgentUnbound("missing investigation deps")
    return deps


# Model


class GuardedModel(Model):
    """Fresh authority and context first; the provider model only after."""

    def __init__(self, binding: AgentBinding) -> None:
        super().__init__()
        self._binding = binding

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        services = self._binding.require()
        deps = current_deps()
        step = await services.steps.prepare_model_step(deps.run_id)
        cause = restart_cause(messages, step)
        if cause is not None:
            # Restart the agent loop; never replay derived claims or tool
            # arguments from a context that is no longer valid.
            _record_restart(deps.run_id, cause)
            raise InvestigationContextChanged(cause)
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
        provenance = {
            "key": step.history_key,
            "evidence": dict(step.evidence_versions),
            "messages": dict(step.history_messages),
            "parts": dict(step.standing.key_parts) if step.standing else {},
        }
        return replace(
            response,
            metadata={**(response.metadata or {}), _HISTORY_PROVENANCE: provenance},
        )

    @property
    def model_name(self) -> str:
        return AGENT_NAME

    @property
    def system(self) -> str:
        return "retail-analytics"

    @property
    def profile(self) -> ModelProfile:
        # The bound provider's profile shapes tool schemas and output mode.
        services = self._binding.services
        return DEFAULT_PROFILE if services is None else services.model.profile


# Most specific first: what a changed key most likely means for the user.
_KEY_CAUSES = (
    (ContextKeyPart.AUTHORITY, ContextRestartCause.AUTHORITY_CHANGED),
    (ContextKeyPart.TOPIC_RESET, ContextRestartCause.TOPIC_RESET),
    (ContextKeyPart.SCHEMA, ContextRestartCause.SCHEMA_CHANGED),
    (ContextKeyPart.PREFERENCES, ContextRestartCause.PREFERENCES_CHANGED),
    (ContextKeyPart.REQUEST, ContextRestartCause.REQUEST_CHANGED),
)


def restart_cause(
    messages: list[ModelMessage], step: ModelStep
) -> ContextRestartCause | None:
    """Why earlier model responses may not be reused, or None if they may.

    Reusable: each response carries provenance with the same key (authority,
    request, preferences, topic reset, approved schema), and everything it was shown -
    evidence versions and history messages - is still valid now according
    to the step's authoritative standing. What one request shows is bounded
    (count, size, scan); something left out of the current prompt is still
    judged by that standing, so leaving it out alone never restarts, while
    invalidation, lost scope, withdrawal or unknown validity always does.
    Without a standing only the current selection is trusted (strict).
    """
    standing = step.standing
    evidence = dict(standing.evidence if standing else step.evidence_versions)
    shown = dict(standing.messages if standing else step.history_messages)
    parts = dict(standing.key_parts) if standing else {}
    for message in messages:
        if not isinstance(message, ModelResponse):
            continue
        previous = (message.metadata or {}).get(_HISTORY_PROVENANCE)
        if (
            not isinstance(previous, dict)
            or not isinstance(previous.get("evidence"), dict)
            or not isinstance(previous.get("messages"), dict)
        ):
            return ContextRestartCause.PROVENANCE_MISSING
        if previous.get("key") != step.history_key:
            return _key_cause(previous.get("parts"), parts)
        if any(
            evidence.get(key) != version
            for key, version in previous["evidence"].items()
        ):
            return ContextRestartCause.EVIDENCE_INVALIDATED
        if any(
            shown.get(key) != fingerprint
            for key, fingerprint in previous["messages"].items()
        ):
            return ContextRestartCause.HISTORY_CHANGED
    return None


def _key_cause(previous: object, current: dict[str, str]) -> ContextRestartCause:
    if isinstance(previous, dict) and previous and current:
        for part, cause in _KEY_CAUSES:
            if previous.get(part) != current.get(part):
                return cause
    return ContextRestartCause.CONTEXT_CHANGED


def _record_restart(run_id: str, cause: ContextRestartCause) -> None:
    """Sanitized cause code only (no context, evidence or message content)."""
    telemetry().count(Metric.CONTEXT_RESTARTS, {Label.REASON: cause.value})
    with telemetry().span(
        Span.CONTEXT_RESTART,
        run_id=run_id,
        attributes={"restart.cause": cause.value},
    ) as span:
        if span.captures:
            span.outputs(
                {
                    "restart_cause": cause.value,
                    "effect": "earlier model turns discarded; the agent loop "
                    "restarts from freshly built context",
                }
            )


# Tools

_ANY_ARGUMENTS = SchemaValidator(schema=core_schema.any_schema())


class CatalogToolset(AbstractToolset[InvestigationDeps]):
    """The executive's permission-filtered catalog, listed per request.

    Arguments are not validated here: the single tool path validates them
    strictly and reports problems as an ``INVALID_INPUT`` result.
    """

    def __init__(self, binding: AgentBinding) -> None:
        self._binding = binding

    @property
    def id(self) -> str:
        return TOOLSET_ID

    async def get_tools(
        self, ctx: RunContext[InvestigationDeps]
    ) -> dict[str, ToolsetTool[InvestigationDeps]]:
        descriptors = await self._binding.require().steps.catalog(ctx.deps.run_id)
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
        result = await self._binding.require().tools.run(
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


# Construction and use


def build_investigation_agent(
    binding: AgentBinding,
    *,
    model: Callable[[AgentBinding], GuardedModel] = GuardedModel,
    toolset: Callable[[AgentBinding], CatalogToolset] = CatalogToolset,
    capabilities: Sequence[AbstractCapability[InvestigationDeps]] = (),
) -> InvestigationAgent:
    """The investigation agent over ``binding``.

    ``model``/``toolset`` let a runtime use a subclass of the guarded model or
    catalog toolset (to translate their typed outcomes); ``capabilities`` add
    runtime behaviour such as durability.
    """

    def catalog(
        ctx: RunContext[InvestigationDeps],
    ) -> AbstractToolset[InvestigationDeps]:
        return toolset(binding)

    return Agent(
        model(binding),
        name=AGENT_NAME,
        deps_type=InvestigationDeps,
        output_type=[AnswerOutput, ClarificationOutput],
        toolsets=[DynamicToolset(catalog, id=TOOLSET_ID)],
        retries=1,
        capabilities=list(capabilities),
    )


async def run_investigation(
    agent: InvestigationAgent, run_id: str
) -> AgentRunResult[InvestigationOutput]:
    """One agent loop over the run's persisted request and rebuilt context."""
    return await agent.run(
        AGENT_PROMPT,
        deps=InvestigationDeps(run_id=run_id),
        usage_limits=UsageLimits(
            request_limit=AGENT_REQUEST_LIMIT, tool_calls_limit=AGENT_TOOL_CALLS_LIMIT
        ),
    )


def proposal(
    run_id: str, sequence: int, result: AgentRunResult[InvestigationOutput]
) -> AnswerDraft | QuestionDraft:
    """The draft to release for the agent's output (``sequence``: release step)."""
    output = result.output
    if isinstance(output, AnswerOutput):
        return AnswerDraft(
            run_id,
            sequence,
            output.text,
            tuple(output.cited_evidence),
            output.complete,
            attribution_from_metadata(
                (result.response.metadata or {}).get(ATTRIBUTION_METADATA_KEY)
            ),
        )
    return QuestionDraft(run_id, sequence, output.question)
