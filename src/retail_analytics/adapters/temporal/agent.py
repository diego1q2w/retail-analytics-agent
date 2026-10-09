"""The shared investigation agent made durable by Temporal.

The agent itself (guarded model, permission-filtered catalog, outputs) is
``adapters.agent.investigator``; this module only adds what Temporal needs:

- ``TemporalDurability``: Pydantic AI runs the agent loop inside the
  workflow and turns every model request and tool call into an activity,
  with the timeouts, heartbeats and retry policies below. The agent is built
  at import time (Temporal registers its activities before any workflow
  runs) over a module-level ``AgentBinding`` that the worker's composition
  root fills (``bind_agent_services``) before the worker starts. Nothing here
  runs I/O in workflow code.
- Error translation at the activity boundary: ``RunStopped`` becomes a
  non-retryable ``RunStopped`` application error, a stale conversation a
  non-retryable ``InvestigationContextChanged`` error and missing services a
  non-retryable ``Unbound`` error. ``interruption`` turns those errors (as
  the workflow sees them) back into the runtime-neutral ``AgentInterruption``
  the application's lifecycle policy decides on.
- The provider budget's request key is scoped to the model activity attempt.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from pydantic import JsonValue
from pydantic_ai import RunContext
from pydantic_ai.durable_exec.temporal import TemporalDurability
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.settings import ModelSettings
from pydantic_ai.toolsets import ToolsetTool
from temporalio import activity
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

from retail_analytics.adapters.agent.investigator import (
    TOOLSET_ID,
    AgentBinding,
    AgentServices,
    AgentUnbound,
    CatalogToolset,
    GuardedModel,
    InvestigationAgent,
    InvestigationDeps,
    build_investigation_agent,
)
from retail_analytics.adapters.models.budgeted import request_scope
from retail_analytics.application.contracts.investigations import (
    AgentInterruption,
    InterruptionKind,
    StopReason,
)
from retail_analytics.application.investigation_runtime import (
    InvestigationContextChanged,
    RunStopped,
)
from retail_analytics.domain.budgets import BudgetResource

# Temporal application error types; recorded in workflow histories.
RUN_STOPPED = "RunStopped"
CONTEXT_CHANGED = "InvestigationContextChanged"
UNBOUND = "Unbound"

# Activity limits. A worker that dies is detected by the heartbeat timeout and
# the activity is retried on another worker; retries are bounded and every
# retry re-checks authority and budgets, and reconciles external effects.
# A model activity covers provider retries and the fallback chain; each
# request has its own response deadlines and the run's active-time budget
# refuses new attempts, so this is only a backstop for a stuck worker.
MODEL_ACTIVITY_TIMEOUT = timedelta(minutes=15)
MODEL_HEARTBEAT_TIMEOUT = timedelta(seconds=30)
TOOL_ACTIVITY_TIMEOUT = timedelta(minutes=10)
TOOL_HEARTBEAT_TIMEOUT = timedelta(seconds=15)
ACTIVITY_ATTEMPTS = 5


def stopped_error(stopped: RunStopped) -> ApplicationError:
    resource = None if stopped.resource is None else stopped.resource.value
    return ApplicationError(
        "investigation stopped",
        stopped.reason.value,
        resource,
        type=RUN_STOPPED,
        non_retryable=True,
    )


def _unbound_error(unbound: AgentUnbound) -> ApplicationError:
    return ApplicationError(str(unbound), type=UNBOUND, non_retryable=True)


def _attempt_scope() -> str | None:
    # Stable per activity attempt; outside an activity every request is new.
    if not activity.in_activity():
        return None
    info = activity.info()
    return f"{info.workflow_id}/{info.activity_id}/{info.attempt}"


class TemporalGuardedModel(GuardedModel):
    """The shared guarded model, inside the model activity."""

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        try:
            with request_scope(_attempt_scope()):
                return await super().request(
                    messages, model_settings, model_request_parameters
                )
        except AgentUnbound as unbound:
            raise _unbound_error(unbound) from None
        except InvestigationContextChanged:
            raise ApplicationError(
                "investigation context changed",
                type=CONTEXT_CHANGED,
                non_retryable=True,
            ) from None
        except RunStopped as stopped:
            raise stopped_error(stopped) from None


class TemporalCatalogToolset(CatalogToolset):
    """The shared catalog toolset, inside the tool activities."""

    async def get_tools(
        self, ctx: RunContext[InvestigationDeps]
    ) -> dict[str, ToolsetTool[InvestigationDeps]]:
        try:
            return await super().get_tools(ctx)
        except AgentUnbound as unbound:
            raise _unbound_error(unbound) from None

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[InvestigationDeps],
        tool: ToolsetTool[InvestigationDeps],
    ) -> dict[str, JsonValue]:
        try:
            return await super().call_tool(name, tool_args, ctx, tool)
        except AgentUnbound as unbound:
            raise _unbound_error(unbound) from None


def _retry(attempts: int) -> RetryPolicy:
    return RetryPolicy(
        initial_interval=timedelta(seconds=1),
        maximum_interval=timedelta(seconds=20),
        maximum_attempts=attempts,
        non_retryable_error_types=[RUN_STOPPED, UNBOUND],
    )


# The worker's registration object, filled by its composition root.
_binding = AgentBinding()


def bind_agent_services(services: AgentServices) -> None:
    """Called once by a worker's composition root before it starts polling."""
    _binding.bind(services)


investigation_agent: InvestigationAgent = build_investigation_agent(
    _binding,
    model=TemporalGuardedModel,
    toolset=TemporalCatalogToolset,
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


def _causes(error: BaseException) -> list[BaseException]:
    """``error`` and its causes through Temporal and agent error wrappers."""
    chain: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and all(current is not seen for seen in chain):
        chain.append(current)
        cause = getattr(current, "cause", None)
        current = cause if isinstance(cause, BaseException) else current.__cause__
    return chain


def is_run_stopped(error: BaseException) -> tuple[str, str | None] | None:
    """(reason, resource) when ``error`` (or a cause) is a ``RunStopped``."""
    for current in _causes(error):
        if isinstance(current, ApplicationError) and current.type == RUN_STOPPED:
            details: Sequence[Any] = current.details
            reason = str(details[0]) if details else "interrupted"
            resource = details[1] if len(details) > 1 else None
            return reason, None if resource is None else str(resource)
    return None


def is_context_changed(error: BaseException) -> bool:
    """Recognize a context restart through Temporal and agent error wrappers."""
    return any(
        isinstance(current, ApplicationError) and current.type == CONTEXT_CHANGED
        for current in _causes(error)
    )


def interruption(error: BaseException) -> AgentInterruption:
    """The runtime-neutral reason an agent run failed with ``error``."""
    if is_context_changed(error):
        return AgentInterruption(InterruptionKind.CONTEXT_CHANGED)
    stopped = is_run_stopped(error)
    if stopped is None:
        return AgentInterruption(InterruptionKind.FAILED)
    reason, resource = stopped
    return AgentInterruption(
        InterruptionKind.STOPPED,
        StopReason(reason),
        BudgetResource(resource) if resource else None,
    )
