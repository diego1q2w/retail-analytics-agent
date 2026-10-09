"""A failed query is traceable from the user-visible event to the sanitized
tool attempt, the query attempt and the compiler rejection."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from duckdb import DuckDBPyConnection as Connection

from retail_analytics.application.contracts.progress import EventKind
from retail_analytics.application.contracts.telemetry import Label, Metric, Span
from retail_analytics.application.query_execution import QueryFailed, QuerySucceeded
from retail_analytics.application.telemetry import use_telemetry
from tests.unit.privacy.support import MASTER_KEY, customer_database
from tests.unit.query_execution.test_service import (
    DERIVED_JOIN,
    TOP_CUSTOMERS,
)
from tests.unit.query_execution.test_service import (
    Harness as QueryHarness,
)
from tests.unit.telemetry.recording import recording
from tests.unit.tools.fakes import SQL
from tests.unit.tools.test_gateway import Harness as GatewayHarness

pytestmark = pytest.mark.asyncio


async def test_failed_tool_call_links_event_attempt_and_sanitized_error() -> None:
    telemetry, sink = recording()
    h = GatewayHarness()
    with use_telemetry(telemetry):
        await h.call(SQL, sql="invalid", purpose="Revenue by product")

    event = h.sink.updates[-1]
    assert event.kind is EventKind.TOOL_FAILED
    assert event.tool is not None and event.correlation.operation_id == "op-7"
    (span,) = sink.named(Span.TOOL)
    # The event's identifiers locate the span; the codes agree.
    assert span.run_id == event.correlation.run_id == span.attributes["run_id"]
    assert span.attributes["operation_id"] == event.correlation.operation_id
    assert span.attributes["session_id"] == event.correlation.session_id
    assert span.attributes["capability"] == event.tool.capability == SQL
    assert span.attributes["attempt"] == event.tool.attempt == 2
    assert event.tool.error_code is not None
    assert span.attributes["error_code"] == event.tool.error_code.value.lower()
    assert span.attributes["error_code"] == "invalid_query"
    assert span.attributes["error_summary"] == "Bad query."
    assert span.failed == "invalid_query"
    assert (
        sink.total(
            Metric.TOOL_CALLS,
            capability=SQL,
            outcome="failed",
            error_code="invalid_query",
        )
        == 1
    )


async def test_unexpected_handler_errors_and_arguments_stay_out_of_telemetry() -> None:
    telemetry, sink = recording()
    h = GatewayHarness()
    with use_telemetry(telemetry):
        await h.call(SQL, sql="raise", purpose="jane@example.com salary")
        await h.call(SQL, sql="SELECT secret FROM t", purpose="p")
        await h.call("drop_everything_for_jane_example_com", x="jane@example.com")
    text = sink.everything()
    assert "jane@example.com" not in text and "leaky" not in text
    # Arguments and model-supplied names are captured content only, never
    # attributes or metric labels.
    metadata = sink.metadata()
    assert "SELECT" not in metadata and "salary" not in metadata
    assert sink.total(Metric.TOOL_CALLS, capability="unknown_tool") == 1
    assert "drop_everything" not in metadata
    spans = sink.named(Span.TOOL)
    assert spans[1].content("inputs") == {
        "tool_call_id": "call-1",
        "operation_id": "op-7",
        "tool": SQL,
        "attempt": 2,
        "arguments": {"sql": "SELECT secret FROM t", "parameters": {}, "purpose": "p"},
    }
    assert "rejected_arguments" in spans[2].content("inputs")  # type: ignore[operator]


@pytest.fixture
def db() -> Iterator[Connection]:
    connection = customer_database()
    yield connection
    connection.close()


async def test_query_attempt_records_job_bytes_and_no_sql_or_secrets(
    db: Connection,
) -> None:
    telemetry, sink = recording()
    h = QueryHarness(db)
    with use_telemetry(telemetry):
        outcome = await h.run()
    assert isinstance(outcome, QuerySucceeded)
    (span,) = sink.named(Span.QUERY)
    assert span.run_id == "run-1" and span.attributes["operation_id"] == "op-0001"
    assert span.attributes["job_id"] == outcome.job.job_id
    assert span.attributes["outcome"] == "succeeded"
    assert span.attributes["record_count"] == len(outcome.result.rows)
    assert span.attributes["bytes_billed"] == outcome.statistics.bytes_billed
    assert sink.total(Metric.QUERIES, outcome="succeeded") == 1
    assert sink.total(Metric.QUERY_BYTES, kind="billed") == (
        outcome.statistics.bytes_billed or 0
    )
    metadata = sink.metadata()
    assert "SELECT" not in metadata and "customer_ref" not in metadata
    assert TOP_CUSTOMERS[:30] not in metadata
    text = sink.everything()
    assert MASTER_KEY.decode() not in text
    for parameter in h.warehouse.jobs[outcome.job.job_id].submission.parameters:
        if parameter.secret or parameter.trusted:
            assert f'"{parameter.value}"' not in text
    (compile_span,) = sink.named(Span.COMPILE)
    inputs = compile_span.content("inputs")
    outputs = compile_span.content("outputs")
    assert isinstance(inputs, dict) and isinstance(outputs, dict)
    assert inputs["generated_sql"].startswith("SELECT s.customer_ref")
    assert outputs["executed_sql"].startswith("SELECT `s`.`customer_ref`")
    assert set(outputs["analysis_parameters"]) == {"status"}
    result = span.content("outputs")
    assert isinstance(result, dict)
    assert result["internal_result"]["row_count"] == len(outcome.result.rows)


async def test_compiler_rejection_is_counted_by_class_and_exception_type(
    db: Connection,
) -> None:
    telemetry, sink = recording()
    h = QueryHarness(db)
    with use_telemetry(telemetry):
        outcome = await h.run(query="SELEKT nonsense FROM WHERE (")
        unsupported = await h.run(query="DELETE FROM sales_items", op="op-0002")
    assert isinstance(outcome, QueryFailed) and isinstance(unsupported, QueryFailed)
    rejections = [
        labels
        for metric, _, labels in sink.counts
        if metric is Metric.COMPILER_REJECTIONS
    ]
    assert len(rejections) == 2
    syntax = next(r for r in rejections if r[Label.REASON] == "syntax_error")
    assert syntax[Label.ERROR_CODE] == "invalid_query"
    assert syntax[Label.CAUSE_TYPE] not in (
        "none",
        "other",
    )  # the parser exception class
    other = next(r for r in rejections if r is not syntax)
    assert other[Label.CAUSE_TYPE] == "none"  # a policy rejection, not an exception
    failed_spans = [s for s in sink.named(Span.QUERY) if s.failed]
    assert {s.attributes["reason"] for s in failed_spans} >= {"syntax_error"}
    assert "SELEKT" not in sink.metadata() and "DELETE" not in sink.metadata()
    rejected = [s.content("outputs") for s in sink.named(Span.COMPILE)]
    assert all(isinstance(r, dict) and "rejected" in r for r in rejected)


async def test_compiler_rejection_is_counted_apart_from_executed_queries(
    db: Connection,
) -> None:
    telemetry, sink = recording()
    h = QueryHarness(db)
    with use_telemetry(telemetry):
        rejected = await h.run(query=DERIVED_JOIN)
        executed = await h.run(op="op-0002")
    assert isinstance(rejected, QueryFailed) and rejected.rejected
    assert isinstance(executed, QuerySucceeded)
    assert sink.total(Metric.QUERIES, outcome="rejected") == 1
    assert sink.total(Metric.QUERIES, outcome="failed") == 0
    assert sink.total(Metric.QUERIES, outcome="succeeded") == 1
    first, _ = sink.named(Span.QUERY)
    assert first.attributes["outcome"] == "rejected"
    assert "job_id" not in first.attributes
