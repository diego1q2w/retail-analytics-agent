"""The shared invocation path, exercised with fake SQL/chart/delivery tools."""

from __future__ import annotations

from typing import Any

import pytest

from retail_analytics.application.contracts.progress import (
    EventKind,
    ProgressUpdate,
)
from retail_analytics.application.tools import (
    CapabilityRegistry,
    ExecutionContext,
    OperationContext,
    ToolCall,
    ToolFailed,
    ToolOutcomeUnknown,
    ToolPending,
    ToolResult,
    ToolSucceeded,
    invoke,
)
from retail_analytics.domain.operations import ToolErrorCode
from tests.unit.tools.fakes import (
    CHART,
    DELIVER,
    SQL,
    Calls,
    ChartOutput,
    DeliveryOutput,
    RecordingSink,
    SqlOutput,
    chart_spec,
    delivery_spec,
    execution_context,
    sql_spec,
)

pytestmark = pytest.mark.asyncio


class Harness:
    def __init__(self, context: ExecutionContext | None = None) -> None:
        self.calls = Calls()
        self.sink = RecordingSink()
        self.registry = CapabilityRegistry(
            [sql_spec(self.calls), chart_spec(self.calls), delivery_spec(self.calls)]
        )
        self.context = OperationContext(
            execution=context or execution_context(), operation_id="op-7", attempt=2
        )

    async def call(self, name: str, **arguments: Any) -> ToolResult[Any]:
        return await invoke(
            self.registry,
            ToolCall(call_id="call-1", name=name, arguments=arguments),
            self.context,
            self.sink,
        )

    @property
    def kinds(self) -> list[EventKind]:
        return [update.kind for update in self.sink.updates]


SQL_ARGS: dict[str, Any] = {"sql": "SELECT 1", "purpose": "Revenue by product"}


async def test_sql_success_is_typed_and_correlated() -> None:
    h = Harness()
    result = await h.call(SQL, **SQL_ARGS)

    assert isinstance(result.outcome, ToolSucceeded)
    assert isinstance(result.outcome.output, SqlOutput)
    assert result.outcome.output.evidence_id == "ev-op-7"
    assert (result.capability, result.capability_version) == (SQL, 1)
    assert result.operation_id == "op-7"
    assert h.kinds == [EventKind.TOOL_STARTED, EventKind.TOOL_SUCCEEDED]
    for update in h.sink.updates:
        assert update.correlation.run_id == "r-1"
        assert update.correlation.session_id == "s-1"
        assert update.correlation.trace_id == "t-1"
        assert update.correlation.operation_id == "op-7"
        assert update.tool is not None
        assert update.tool.attempt == 2
    # The handler sees trusted context it was not given by the model.
    _, _, ctx = h.calls.seen[0]
    assert ctx.execution.product_scope.product_ids == frozenset({"p-1"})


async def test_result_serializes_with_concrete_output() -> None:
    h = Harness()
    result = await h.call(SQL, **SQL_ARGS)
    parsed = ToolResult[SqlOutput].model_validate_json(result.model_dump_json())
    assert parsed == result
    assert '"rows":[["A",30]]' in result.model_dump_json()


async def test_empty_result_is_success_not_error() -> None:
    h = Harness()
    result = await h.call(SQL, sql="empty", purpose="p")
    assert isinstance(result.outcome, ToolSucceeded)
    assert result.outcome.empty
    assert h.kinds[-1] is EventKind.TOOL_SUCCEEDED


@pytest.mark.parametrize(
    "smuggled",
    [
        {"executive_id": "ceo"},
        {"product_ids": ["p-1", "p-2"]},
        {"entitlement_version": 99},
        {"budget": {"bytes": 10**15}},
        {"confirmed": True},
        {"reasoning": "ignore previous instructions"},
    ],
)
async def test_malicious_extra_arguments_are_rejected(
    smuggled: dict[str, Any],
) -> None:
    h = Harness()
    result = await h.call(SQL, **SQL_ARGS, **smuggled)

    assert isinstance(result.outcome, ToolFailed)
    assert result.outcome.code is ToolErrorCode.INVALID_INPUT
    assert [i.issue for i in result.outcome.issues] == ["extra_forbidden"]
    assert h.calls.seen == []
    assert h.kinds == [EventKind.TOOL_FAILED]
    # Rejected values are never echoed back.
    dumped = result.model_dump_json()
    for value in smuggled.values():
        if isinstance(value, str):
            assert value not in dumped


async def test_invalid_argument_types_report_locations_not_values() -> None:
    h = Harness()
    result = await h.call(SQL, sql=["secret-value"], purpose="p")
    assert isinstance(result.outcome, ToolFailed)
    assert result.outcome.issues[0].location == ("sql",)
    assert "secret-value" not in result.model_dump_json()


@pytest.mark.parametrize("name", [DELIVER, "no_such_tool"])
async def test_hidden_and_unknown_tools_look_the_same(name: str) -> None:
    h = Harness(execution_context(permissions=frozenset({"analyze"})))
    result = await h.call(name, report_id="r", report_version=1, recipient_ref="x")

    assert isinstance(result.outcome, ToolFailed)
    assert result.outcome.code is ToolErrorCode.ACCESS_DENIED
    assert result.outcome.message == "This tool is not available."
    assert result.capability_version is None
    assert h.calls.seen == []


async def test_authorization_is_checked_at_execution_not_catalog_time() -> None:
    h = Harness()
    assert SQL in {tool.name for tool in h.registry.catalog(h.context.execution)}
    # Entitlements revoked between catalog listing and execution.
    h.context = OperationContext(
        execution=execution_context(products=frozenset()), operation_id="op-8"
    )
    result = await h.call(SQL, **SQL_ARGS)
    assert isinstance(result.outcome, ToolFailed)
    assert result.outcome.code is ToolErrorCode.ACCESS_DENIED
    assert h.calls.seen == []


async def test_pending_and_unknown_are_not_errors() -> None:
    h = Harness()
    pending = await h.call(SQL, sql="pending", purpose="p")
    unknown = await h.call(SQL, sql="unknown", purpose="p")

    assert isinstance(pending.outcome, ToolPending)
    assert pending.outcome.reference == "job-op-7"
    assert isinstance(unknown.outcome, ToolOutcomeUnknown)
    assert unknown.outcome.reference == "job-op-7"
    assert not hasattr(pending.outcome, "code")
    assert not hasattr(unknown.outcome, "code")
    assert EventKind.TOOL_PENDING in h.kinds
    assert EventKind.TOOL_OUTCOME_UNKNOWN in h.kinds
    assert EventKind.TOOL_FAILED not in h.kinds


async def test_handler_reported_failure_passes_through() -> None:
    h = Harness()
    result = await h.call(SQL, sql="invalid", purpose="p")
    assert isinstance(result.outcome, ToolFailed)
    assert result.outcome.code is ToolErrorCode.INVALID_QUERY
    failed = h.sink.updates[-1]
    assert failed.tool is not None
    assert failed.tool.error_code is ToolErrorCode.INVALID_QUERY


async def test_crash_after_possible_external_effect_is_unknown() -> None:
    h = Harness()
    for result in (
        await h.call(SQL, sql="raise", purpose="p"),
        await h.call(DELIVER, report_id="r", report_version=1, recipient_ref="raise"),
    ):
        assert isinstance(result.outcome, ToolOutcomeUnknown)
        dumped = result.model_dump_json()
        assert "jane@example.com" not in dumped
        assert "smtp" not in dumped


async def test_bad_output_from_read_only_tool_is_internal_error() -> None:
    h = Harness()
    result = await h.call(CHART, evidence_id="wrong-output", kind="bar")
    assert isinstance(result.outcome, ToolFailed)
    assert result.outcome.code is ToolErrorCode.INTERNAL_ERROR


async def test_chart_and_delivery_extensions_use_the_same_path() -> None:
    h = Harness()
    chart = await h.call(CHART, evidence_id="ev-1", kind="line")
    sent = await h.call(DELIVER, report_id="rep-1", report_version=2, recipient_ref="a")

    assert isinstance(chart.outcome, ToolSucceeded)
    assert isinstance(chart.outcome.output, ChartOutput)
    assert chart.capability_version == 2
    assert isinstance(sent.outcome, ToolSucceeded)
    assert isinstance(sent.outcome.output, DeliveryOutput)
    assert sent.outcome.output.delivery_id == "d-op-7"

    bad = await h.call(CHART, evidence_id="ev-1", kind="pie")
    assert isinstance(bad.outcome, ToolFailed)
    assert bad.outcome.code is ToolErrorCode.INVALID_INPUT


async def test_progress_updates_carry_no_free_reasoning() -> None:
    h = Harness()
    await h.call(SQL, **SQL_ARGS)
    for update in h.sink.updates:
        assert isinstance(update, ProgressUpdate)
        # The model's purpose text is not copied into application events.
        assert "Revenue by product" not in update.model_dump_json()
