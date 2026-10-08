"""Serialization and strictness of tool and progress contracts."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from retail_analytics.application.contracts import CONTRACT_VERSION, Correlation
from retail_analytics.application.progress import (
    EventKind,
    EventSource,
    InputRequest,
    ProgressEvent,
    ProgressUpdate,
    ToolActivity,
)
from retail_analytics.application.tools import (
    OperationContext,
    ToolCall,
    ToolFailed,
    ToolOutcomeUnknown,
    ToolPending,
    ToolResult,
    ToolSucceeded,
)
from retail_analytics.domain.operations import ToolErrorCode
from tests.unit.tools.fakes import SqlOutput, execution_context

RUN = Correlation(session_id="s-1", run_id="r-1", trace_id="t-1")
OP = RUN.model_copy(update={"operation_id": "op-1"})
PRIVATE_REASONING = ("reasoning", "thinking", "thought", "chain_of_thought")


def _result(outcome: Any) -> ToolResult[SqlOutput]:
    return ToolResult[SqlOutput](
        call_id="c-1",
        capability="execute_analysis",
        capability_version=1,
        operation_id="op-1",
        outcome=outcome,
    )


OUTCOMES: list[Any] = [
    ToolSucceeded(
        output=SqlOutput(columns=("a",), rows=((1,),), truncated=True, evidence_id="e")
    ),
    ToolPending(reference="job-1", summary="Queued."),
    ToolOutcomeUnknown(reference="job-1"),
    ToolFailed(code=ToolErrorCode.BUDGET_EXCEEDED, message="Query too large."),
]


@pytest.mark.parametrize("outcome", OUTCOMES)
def test_tool_result_round_trips(outcome: Any) -> None:
    result = _result(outcome)
    data = json.loads(result.model_dump_json())
    assert data["schema_version"] == CONTRACT_VERSION
    assert ToolResult[SqlOutput].model_validate(data) == result


def test_statuses_are_distinct() -> None:
    statuses = [
        json.loads(_result(o).model_dump_json())["outcome"]["status"] for o in OUTCOMES
    ]
    assert statuses == ["succeeded", "pending", "unknown", "failed"]


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ((), {"executive_id": "ceo"}),
        (("outcome",), {"reasoning": "secret"}),
        (("outcome", "output"), {"product_scope": ["all"]}),
    ],
)
def test_result_rejects_extra_fields(
    path: tuple[str, ...], value: dict[str, Any]
) -> None:
    data = json.loads(_result(OUTCOMES[0]).model_dump_json())
    target = data
    for key in path:
        target = target[key]
    target.update(value)
    with pytest.raises(ValidationError, match=r"Extra inputs"):
        ToolResult[SqlOutput].model_validate(data)


def test_result_rejects_other_schema_version_and_status() -> None:
    data = json.loads(_result(OUTCOMES[1]).model_dump_json())
    with pytest.raises(ValidationError):
        ToolResult[SqlOutput].model_validate({**data, "schema_version": 2})
    with pytest.raises(ValidationError):
        ToolResult[SqlOutput].model_validate(
            {**data, "outcome": {"status": "approved", "reference": "x"}}
        )


def test_tool_call_is_strict() -> None:
    call = ToolCall(call_id="c-1", name="execute_analysis", arguments={"sql": "x"})
    assert ToolCall.model_validate_json(call.model_dump_json()) == call
    with pytest.raises(ValidationError):
        ToolCall.model_validate({**call.model_dump(), "executive_id": "ceo"})
    with pytest.raises(ValidationError):
        ToolCall.model_validate({**call.model_dump(), "name": "../admin"})


def test_failure_message_is_bounded() -> None:
    with pytest.raises(ValidationError):
        ToolFailed(code=ToolErrorCode.INVALID_QUERY, message="x" * 501)


def test_operation_context_correlation() -> None:
    ctx = OperationContext(execution=execution_context(), operation_id="op-1")
    assert ctx.correlation == Correlation(
        session_id="s-1", run_id="r-1", trace_id="t-1", operation_id="op-1"
    )
    assert ctx.next_attempt().attempt == 2
    with pytest.raises(ValidationError):
        OperationContext(execution=execution_context(), operation_id="bad id!")


def _tool_update(**overrides: Any) -> dict[str, Any]:
    return {
        "correlation": OP.model_dump(),
        "kind": "tool.started",
        "summary": "Running an analysis query.",
        "tool": {"capability": "execute_analysis", "capability_version": 1},
        **overrides,
    }


def test_progress_event_round_trips_with_correlation_ids() -> None:
    update = ProgressUpdate.model_validate(_tool_update())
    event = ProgressEvent.stamp(
        update,
        event_id="ev-1",
        sequence=4,
        occurred_at=datetime(2026, 10, 8, tzinfo=UTC),
    )
    data = json.loads(event.model_dump_json())
    assert data["correlation"] == {
        "session_id": "s-1",
        "run_id": "r-1",
        "trace_id": "t-1",
        "operation_id": "op-1",
    }
    assert (data["event_id"], data["sequence"]) == ("ev-1", 4)
    assert ProgressEvent.model_validate(data) == event


def test_progress_schema_has_no_private_reasoning_field() -> None:
    schema = json.dumps(ProgressEvent.model_json_schema()).lower()
    for name in PRIVATE_REASONING:
        assert name not in schema
    for name in PRIVATE_REASONING:
        with pytest.raises(ValidationError):
            ProgressUpdate.model_validate(_tool_update(**{name: "because..."}))


def test_progress_event_requires_aware_time() -> None:
    update = ProgressUpdate.model_validate(_tool_update())
    with pytest.raises(ValidationError):
        ProgressEvent.stamp(
            update, event_id="e", sequence=1, occurred_at=datetime(2026, 1, 1)
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"tool": None},
        {"correlation": RUN.model_dump()},
        {"kind": "analysis.progress"},
        {"kind": "tool.failed"},
        {
            "kind": "tool.succeeded",
            "tool": {
                "capability": "execute_analysis",
                "capability_version": 1,
                "error_code": "ACCESS_DENIED",
            },
        },
        {"source": "model"},
        {"summary": "x" * 281},
        {"summary": ""},
    ],
)
def test_inconsistent_progress_is_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ProgressUpdate.model_validate(_tool_update(**overrides))


def test_model_may_only_supply_analysis_summaries() -> None:
    update = ProgressUpdate(
        correlation=RUN,
        kind=EventKind.ANALYSIS_PROGRESS,
        source=EventSource.MODEL,
        summary="The decline is concentrated in two categories.",
    )
    assert update.tool is None
    with pytest.raises(ValidationError):
        ProgressUpdate(
            correlation=RUN,
            kind=EventKind.RUN_COMPLETED,
            source=EventSource.MODEL,
            summary="Done, deletion approved.",
        )


def test_input_required_carries_question() -> None:
    question = InputRequest(question_id="q-1", question="Exclude returns?")
    update = ProgressUpdate(
        correlation=RUN,
        kind=EventKind.INPUT_REQUIRED,
        summary="Waiting for your answer.",
        input_request=question,
    )
    assert update.input_request == question
    with pytest.raises(ValidationError):
        ProgressUpdate(
            correlation=RUN, kind=EventKind.INPUT_REQUIRED, summary="Waiting."
        )


def test_tool_activity_failed_event() -> None:
    update = ProgressUpdate(
        correlation=OP,
        kind=EventKind.TOOL_FAILED,
        summary="Query too large.",
        tool=ToolActivity(
            capability="execute_analysis",
            capability_version=1,
            error_code=ToolErrorCode.BUDGET_EXCEEDED,
        ),
    )
    assert update.tool is not None
    assert update.tool.error_code is ToolErrorCode.BUDGET_EXCEEDED
