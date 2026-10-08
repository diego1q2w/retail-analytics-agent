"""The single path from a model tool call to a capability handler.

Every capability goes through the same steps, so a new one cannot skip them:
fresh authorization against the trusted context, strict argument validation,
progress events, output validation and conversion of unexpected failures into
an honest outcome. Budget checks and durable operation recording wrap this
call; they are not capability concerns either.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from retail_analytics.application.progress import (
    EventKind,
    ProgressSink,
    ProgressUpdate,
    ToolActivity,
)
from retail_analytics.application.tools.context import OperationContext
from retail_analytics.application.tools.contracts import (
    InputIssue,
    ToolCall,
    ToolFailed,
    ToolOutcomeUnknown,
    ToolPending,
    ToolResult,
    ToolSucceeded,
)
from retail_analytics.application.tools.registry import (
    CapabilityRegistry,
    CapabilitySpec,
)
from retail_analytics.domain.operations import ToolErrorCode

MAX_ISSUES = 20
_UNAVAILABLE = "This tool is not available."
_INVALID_INPUT = "Arguments do not match the tool's input schema."
_INTERNAL = "The tool failed unexpectedly and produced no result."
_UNVERIFIED = (
    "The tool's outcome could not be verified; it will be reconciled before any retry."
)

type _Outcome = ToolSucceeded[Any] | ToolPending | ToolOutcomeUnknown | ToolFailed


async def invoke(
    registry: CapabilityRegistry,
    call: ToolCall,
    context: OperationContext,
    progress: ProgressSink,
) -> ToolResult[Any]:
    spec = registry.resolve(call.name, context.execution)
    if spec is None:
        failed = ToolFailed(code=ToolErrorCode.ACCESS_DENIED, message=_UNAVAILABLE)
        await progress.publish(_terminal_update(call.name, None, context, failed))
        return ToolResult[Any](
            call_id=call.call_id,
            capability=call.name,
            capability_version=None,
            operation_id=context.operation_id,
            outcome=failed,
        )

    outcome = await _run(spec, call, context, progress)
    await progress.publish(_terminal_update(spec.name, spec.version, context, outcome))
    return ToolResult[spec.output_model](  # type: ignore[name-defined]
        call_id=call.call_id,
        capability=spec.name,
        capability_version=spec.version,
        operation_id=context.operation_id,
        outcome=outcome,
    )


async def _run(
    spec: CapabilitySpec[Any, Any],
    call: ToolCall,
    context: OperationContext,
    progress: ProgressSink,
) -> _Outcome:
    try:
        arguments = spec.input_model.model_validate(call.arguments)
    except ValidationError as error:
        return ToolFailed(
            code=ToolErrorCode.INVALID_INPUT,
            message=_INVALID_INPUT,
            issues=_issues(error),
        )

    await progress.publish(
        ProgressUpdate(
            correlation=context.correlation,
            kind=EventKind.TOOL_STARTED,
            summary=spec.progress_label,
            tool=_activity(spec.name, spec.version, context),
        )
    )
    try:
        outcome: object = await spec.handler(arguments, context)
    except Exception:
        # Deliberately not echoing the exception: it may contain data values.
        return _unverified(spec)
    return _checked(spec, outcome)


def _checked(spec: CapabilitySpec[Any, Any], outcome: object) -> _Outcome:
    if isinstance(outcome, ToolPending | ToolOutcomeUnknown | ToolFailed):
        return outcome
    if isinstance(outcome, ToolSucceeded) and type(outcome.output) is spec.output_model:
        try:
            output = spec.output_model.model_validate(outcome.output.model_dump())
        except ValidationError:
            return _unverified(spec)
        return ToolSucceeded[spec.output_model](  # type: ignore[name-defined]
            output=output, empty=outcome.empty
        )
    return _unverified(spec)


def _unverified(spec: CapabilitySpec[Any, Any]) -> ToolFailed | ToolOutcomeUnknown:
    """A failure with no trustworthy result.

    For read-only work nothing can have changed, so it is an internal error.
    If an external effect might exist, claiming failure would invite a
    duplicate, so the outcome is unknown and must be reconciled.
    """
    if spec.side_effect.may_leave_external_effect:
        return ToolOutcomeUnknown(summary=_UNVERIFIED)
    return ToolFailed(code=ToolErrorCode.INTERNAL_ERROR, message=_INTERNAL)


def _issues(error: ValidationError) -> tuple[InputIssue, ...]:
    return tuple(
        InputIssue(
            location=tuple(
                part if isinstance(part, int) else str(part)[:64]
                for part in detail["loc"]
            ),
            issue=detail["type"][:64],
        )
        for detail in error.errors(include_url=False, include_input=False)[:MAX_ISSUES]
    )


def _activity(
    name: str,
    version: int | None,
    context: OperationContext,
    error_code: ToolErrorCode | None = None,
) -> ToolActivity:
    return ToolActivity(
        capability=name,
        capability_version=version,
        attempt=context.attempt,
        error_code=error_code,
    )


def _terminal_update(
    name: str, version: int | None, context: OperationContext, outcome: _Outcome
) -> ProgressUpdate:
    error_code = None
    match outcome:
        case ToolSucceeded():
            kind = EventKind.TOOL_SUCCEEDED
            summary = "No matching data." if outcome.empty else "Completed."
        case ToolPending():
            kind = EventKind.TOOL_PENDING
            summary = outcome.summary or "Still running; checking again later."
        case ToolOutcomeUnknown():
            kind = EventKind.TOOL_OUTCOME_UNKNOWN
            summary = outcome.summary or _UNVERIFIED
        case ToolFailed():
            kind = EventKind.TOOL_FAILED
            summary = outcome.message
            error_code = outcome.code
    return ProgressUpdate(
        correlation=context.correlation,
        kind=kind,
        summary=summary[:280],
        tool=_activity(name, version, context, error_code),
    )
