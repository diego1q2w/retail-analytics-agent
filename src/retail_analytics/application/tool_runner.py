"""Running one model tool call inside a retryable activity.

``ToolRunner.run`` is what the durable runtime executes for every tool call
the model makes. It is safe to repeat (an activity retry after a worker crash
runs it again with the same tool-call ID) because:

- the operation ID is derived from the run and the tool-call ID, so a repeat
  addresses the same durable operation and its idempotency key;
- authority is re-resolved from the run's recorded principal on every attempt
  (never from the model, a token or a workflow payload), then the call goes
  through ``application.tools.invoke`` - the single tool path with catalog
  authorization, strict argument validation and progress events;
- capabilities that may leave an external effect (warehouse jobs) own their
  operation record and reconcile before resubmitting; for the others the
  runner records the operation, and a capability that must not be retried is
  never re-executed once an attempt started;
- every attempt checks the run is still running and that its time budget is
  not spent; transient failures are retried only as ``RunBudgets`` allows,
  counting attempts from the persisted operation history and failures that
  happened before an operation existed;
- a pending or unknown outcome of an external job is followed (the capability
  reconciles its recorded job) until it settles or the run's limits stop it.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import JsonValue

from retail_analytics.application.authorization import (
    AccessDenied,
    AccessResolver,
)
from retail_analytics.application.budgets import RunBudgets, budget_message
from retail_analytics.application.contracts.persistence import OperationRequest
from retail_analytics.application.contracts.progress import (
    EventKind,
    ProgressUpdate,
    ToolActivity,
)
from retail_analytics.application.contracts.telemetry import Label, Metric
from retail_analytics.application.ports.investigations import RunPrincipals
from retail_analytics.application.ports.persistence import (
    RunRepository,
    ToolExecutionRepository,
)
from retail_analytics.application.ports.progress import ProgressSink
from retail_analytics.application.telemetry import telemetry
from retail_analytics.application.tools import (
    CapabilityRegistry,
    CapabilitySpec,
    OperationContext,
    ToolCall,
    ToolFailed,
    ToolOutcomeUnknown,
    ToolPending,
    ToolResult,
    ToolSucceeded,
    invoke,
)
from retail_analytics.domain.budgets import BudgetResource
from retail_analytics.domain.executions import (
    ToolExecution,
    ToolExecutionStatus,
    transient_failures,
)
from retail_analytics.domain.investigations import operation_id_for
from retail_analytics.domain.operations import RecoveryMode, ToolErrorCode
from retail_analytics.domain.runs import RunStatus

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CALL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_UNAVAILABLE = "This tool is not available."
_STOPPED = "The investigation is no longer running; no new work starts."
_INTERNAL = ToolErrorCode.INTERNAL_ERROR
_NOT_REPEATED = (
    "This tool was interrupted after it started and is not repeated "
    "automatically; its outcome is unknown."
)
_STOPPING_TIME = frozenset({BudgetResource.ACTIVE_TIME})


@dataclass(frozen=True, slots=True)
class ToolRunnerSettings:
    # Seconds between checks of a pending or unknown external job.
    follow_seconds: float = 2.0
    # Upper bound on attempts/checks within one activity (backstop; the run's
    # time budget and the query deadline normally end it first).
    max_iterations: int = 200

    def __post_init__(self) -> None:
        if self.follow_seconds < 0 or self.max_iterations < 1:
            raise ValueError("invalid tool runner settings")


class ToolRunner:
    def __init__(
        self,
        *,
        registry: CapabilityRegistry,
        resolver: AccessResolver,
        principals: RunPrincipals,
        runs: RunRepository,
        operations: ToolExecutionRepository,
        budgets: RunBudgets,
        progress: ProgressSink,
        settings: ToolRunnerSettings | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._registry = registry
        self._resolver = resolver
        self._principals = principals
        self._runs = runs
        self._operations = operations
        self._budgets = budgets
        self._progress = progress
        self._settings = settings or ToolRunnerSettings()
        self._sleep = sleep

    async def run(
        self,
        run_id: str,
        tool_call_id: str,
        name: str,
        arguments: dict[str, JsonValue],
    ) -> ToolResult[Any]:
        operation_id = operation_id_for(run_id, tool_call_id)
        call_id = tool_call_id if _CALL_ID.fullmatch(tool_call_id) else operation_id
        if not _NAME.fullmatch(name):
            return _failed("unknown_tool", call_id, operation_id, _UNAVAILABLE)
        call = ToolCall(call_id=call_id, name=name, arguments=arguments)
        principal = await self._principals.get(run_id)
        if principal is None:
            return _failed(name, call_id, operation_id, _UNAVAILABLE)

        local_failures = 0
        result: ToolResult[Any] | None = None
        for _ in range(self._settings.max_iterations):
            stop = await self._stop_reason(run_id)
            if stop is not None:
                return stop.result(name, call_id, operation_id)
            try:
                context = await self._resolver.context_for_run(
                    principal, run_id, trace_id=run_id
                )
            except AccessDenied:
                return _failed(name, call_id, operation_id, _UNAVAILABLE)
            context = await self._budgets.with_budget(context)
            spec = self._registry.resolve(name, context)
            existing = await self._operations.get(operation_id)
            attempt = (existing.attempt_count if existing else 0) + 1
            op_context = OperationContext(context, operation_id, max(attempt, 1))
            if spec is None:
                # The gateway refuses it the same way and records the event.
                return await invoke(self._registry, call, op_context, self._progress)

            owns_record = not spec.side_effect.may_leave_external_effect
            if owns_record:
                refused = await self._begin(spec, run_id, existing, op_context)
                if refused is not None:
                    return refused.result(name, call_id, operation_id)

            result = await invoke(self._registry, call, op_context, self._progress)
            outcome = result.outcome
            if owns_record:
                await self._record(spec, op_context, outcome)

            match outcome:
                case ToolSucceeded():
                    return result
                case ToolPending() | ToolOutcomeUnknown():
                    if not spec.side_effect.may_leave_external_effect:
                        return result
                    current = await self._operations.get(operation_id)
                    if current is not None and current.status.is_terminal:
                        return result
                    await self._sleep(self._settings.follow_seconds)
                    continue
                case ToolFailed() if outcome.code is ToolErrorCode.TEMPORARY_FAILURE:
                    if spec.retry.recovery is RecoveryMode.NO_RETRY:
                        return result
                    local_failures += 1
                    history = await self._operations.history(operation_id)
                    failures = max(transient_failures(history), local_failures)
                    decision = await self._budgets.retry_decision(run_id, failures)
                    if not decision.allowed:
                        return result
                    telemetry().count(
                        Metric.TOOL_RETRIES, {Label.CAPABILITY: spec.name}
                    )
                    await self._progress.publish(
                        ProgressUpdate(
                            correlation=op_context.correlation,
                            kind=EventKind.TOOL_RETRYING,
                            summary="A temporary failure occurred; retrying shortly.",
                            tool=ToolActivity(
                                capability=spec.name,
                                capability_version=spec.version,
                                attempt=op_context.attempt,
                            ),
                        )
                    )
                    await self._sleep(decision.delay_seconds)
                    continue
                case _:
                    return result
        if result is None:
            return _failed(name, call_id, operation_id, _STOPPED, _INTERNAL)
        return result

    async def _stop_reason(self, run_id: str) -> _Refusal | None:
        run = await self._runs.get_run(run_id)
        if run is None or run.status is not RunStatus.RUNNING:
            return _Refusal(ToolErrorCode.INTERNAL_ERROR, _STOPPED)
        snapshot = await self._budgets.snapshot(run_id)
        if snapshot is not None:
            spent = snapshot.exhausted() & _STOPPING_TIME
            if spent:
                return _Refusal(
                    ToolErrorCode.BUDGET_EXCEEDED, budget_message(next(iter(spent)))
                )
        return None

    async def _begin(
        self,
        spec: CapabilitySpec[Any, Any],
        run_id: str,
        existing: ToolExecution | None,
        ctx: OperationContext,
    ) -> _Refusal | _Unknown | None:
        if existing is None:
            started = await self._operations.begin(
                OperationRequest(
                    operation_id=ctx.operation_id,
                    run_id=run_id,
                    capability=spec.name,
                    capability_version=spec.version,
                    side_effect=spec.side_effect,
                )
            )
            existing = started.execution
        if existing.run_id != run_id:
            return _Refusal(ToolErrorCode.ACCESS_DENIED, _UNAVAILABLE)
        if existing.attempt_count > 0 and spec.retry.recovery is RecoveryMode.NO_RETRY:
            # Single-attempt capabilities are never re-executed by a retry.
            return _Unknown(ctx.operation_id)
        if not existing.status.is_terminal:
            await self._operations.transition(
                ctx.operation_id, ToolExecutionStatus.RUNNING, attempt=ctx.attempt
            )
        return None

    async def _record(
        self,
        spec: CapabilitySpec[Any, Any],
        ctx: OperationContext,
        outcome: object,
    ) -> None:
        current = await self._operations.get(ctx.operation_id)
        if current is None or current.status.is_terminal:
            return
        match outcome:
            case ToolSucceeded():
                await self._operations.transition(
                    ctx.operation_id, ToolExecutionStatus.SUCCEEDED, attempt=ctx.attempt
                )
            case ToolFailed() if (
                outcome.code is ToolErrorCode.TEMPORARY_FAILURE
                and spec.retry.recovery is not RecoveryMode.NO_RETRY
            ):
                await self._operations.transition(
                    ctx.operation_id,
                    ToolExecutionStatus.RETRYING,
                    attempt=ctx.attempt,
                    error_code=outcome.code,
                    detail="transient_failure",
                )
            case ToolFailed():
                await self._operations.transition(
                    ctx.operation_id,
                    ToolExecutionStatus.FAILED,
                    attempt=ctx.attempt,
                    error_code=outcome.code,
                    detail="tool_failed",
                )
            case _:
                await self._operations.transition(
                    ctx.operation_id,
                    ToolExecutionStatus.OUTCOME_UNKNOWN,
                    attempt=ctx.attempt,
                )


@dataclass(frozen=True, slots=True)
class _Refusal:
    code: ToolErrorCode
    message: str

    def result(self, name: str, call_id: str, operation_id: str) -> ToolResult[Any]:
        return _failed(name, call_id, operation_id, self.message, self.code)


@dataclass(frozen=True, slots=True)
class _Unknown:
    operation_id: str

    def result(self, name: str, call_id: str, operation_id: str) -> ToolResult[Any]:
        return ToolResult[Any](
            call_id=call_id,
            capability=name,
            capability_version=None,
            operation_id=operation_id,
            outcome=ToolOutcomeUnknown(reference=operation_id, summary=_NOT_REPEATED),
        )


def _failed(
    name: str,
    call_id: str,
    operation_id: str,
    message: str,
    code: ToolErrorCode = ToolErrorCode.ACCESS_DENIED,
) -> ToolResult[Any]:
    return ToolResult[Any](
        call_id=call_id,
        capability=name,
        capability_version=None,
        operation_id=operation_id,
        outcome=ToolFailed(code=code, message=message),
    )


def compact_result(result: ToolResult[Any]) -> dict[str, JsonValue]:
    """The JSON a model receives for a tool result (and workflow history holds).

    Outputs are the capabilities' own bounded contracts; nothing here adds
    rows or identifiers beyond them.
    """
    dumped: dict[str, JsonValue] = result.model_dump(mode="json", exclude_none=True)
    return dumped


__all__ = ["ToolRunner", "ToolRunnerSettings", "compact_result"]
