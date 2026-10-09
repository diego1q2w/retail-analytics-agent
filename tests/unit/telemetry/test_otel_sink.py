"""The OpenTelemetry sink: correlation, MLflow span types, bounded exporters."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager

import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from retail_analytics.adapters.telemetry.otel import OtelSettings, OtelSink
from retail_analytics.application.contracts.telemetry import Label, Metric, Span
from retail_analytics.application.telemetry import (
    Telemetry,
    root_span_id_for,
    trace_id_for,
)


def settings(**overrides: object) -> OtelSettings:
    base = {
        "service": "retail-analytics-test",
        "instance": "test-1",
        "traces_endpoint": "http://127.0.0.1:1/v1/traces",
        "metrics_endpoint": "http://127.0.0.1:1/v1/metrics",
        "export_timeout_seconds": 1.0,
        "trace_delay_seconds": 0.05,
        "metric_interval_seconds": 1.0,
        "cooloff_seconds": 30.0,
    }
    return OtelSettings(**{**base, **overrides})  # type: ignore[arg-type]


def attrs(span: ReadableSpan) -> dict[str, object]:
    return dict(span.attributes or {})


def parent_id(span: ReadableSpan) -> int | None:
    return None if span.parent is None else span.parent.span_id


def captured() -> tuple[
    Telemetry, InMemorySpanExporter, InMemoryMetricReader, OtelSink
]:
    exporter = InMemorySpanExporter()
    reader = InMemoryMetricReader()
    sink = OtelSink(settings(), span_exporter=exporter, metric_reader=reader)
    # Export synchronously so assertions need no waiting.
    sink._traces.add_span_processor(SimpleSpanProcessor(exporter))
    return Telemetry(sink), exporter, reader, sink


def test_spans_of_a_run_share_its_trace_and_root() -> None:
    telemetry, exporter, _, sink = captured()
    run_id = "run_0123456789abcdef"
    with telemetry.span(Span.ACCEPT, run_id=run_id, attributes={"run_id": run_id}):
        pass
    with (
        telemetry.span(Span.TOOL, run_id=run_id, attributes={"operation_id": "op_1"}),
        telemetry.span(Span.QUERY, run_id=run_id),
    ):
        pass
    with telemetry.span(
        Span.RUN, run_id=run_id, root=True, attributes={"status": "completed"}
    ):
        pass
    sink.flush(1)
    spans = {s.name: s for s in exporter.get_finished_spans()}
    trace = int(trace_id_for(run_id), 16)
    root_id = int(root_span_id_for(run_id), 16)
    assert {s.context.trace_id for s in spans.values()} == {trace}
    root = spans[Span.RUN]
    assert root.context.span_id == root_id and root.parent is None
    assert parent_id(spans[Span.ACCEPT]) == root_id
    assert parent_id(spans[Span.TOOL]) == root_id
    # Nested spans of the same run nest natively.
    assert parent_id(spans[Span.QUERY]) == spans[Span.TOOL].context.span_id
    assert attrs(spans[Span.TOOL])["operation_id"] == "op_1"
    assert attrs(spans[Span.TOOL])["mlflow.spanType"] == '"TOOL"'
    assert attrs(spans[Span.RUN])["mlflow.spanType"] == '"AGENT"'
    assert attrs(spans[Span.TOOL])["run_trace_id"] == trace_id_for(run_id)
    assert (
        spans[Span.TOOL].resource.attributes["service.name"] == "retail-analytics-test"
    )


def test_a_failing_span_is_marked_with_the_exception_type_only() -> None:
    telemetry, exporter, _, _ = captured()
    with (
        pytest.raises(ValueError, match="canary"),
        telemetry.span(Span.TOOL, run_id="run_1"),
    ):
        raise ValueError("canary maria@example.com")
    (span,) = exporter.get_finished_spans()
    assert attrs(span)["error.type"] == "ValueError"
    assert span.status.description == "ValueError"
    assert "maria" not in repr(attrs(span)) + str(span.status.description)
    assert not span.events


def test_unrelated_spans_get_their_own_traces() -> None:
    telemetry, exporter, _, _ = captured()
    with telemetry.span(Span.HTTP):
        pass
    with telemetry.span(Span.HTTP):
        pass
    first, second = exporter.get_finished_spans()
    assert first.context.trace_id != second.context.trace_id


def test_metrics_are_recorded_with_only_allowed_labels() -> None:
    telemetry, _, reader, _ = captured()
    telemetry.count(
        Metric.TOOL_CALLS,
        {Label.CAPABILITY: "execute_analysis", Label.OUTCOME: "failed"},
    )
    telemetry.count(Metric.QUERY_BYTES, {Label.KIND: "billed"}, 2048)
    telemetry.observe(Metric.TOOL_SECONDS, 1.5, {Label.CAPABILITY: "execute_analysis"})
    data = reader.get_metrics_data()
    assert data is not None
    found = {
        metric.name: metric
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }
    assert {"ra_tool_calls", "ra_query_bytes", "ra_tool"} <= set(found)
    assert found["ra_tool"].unit == "s"
    point = found["ra_tool_calls"].data.data_points[0]
    assert dict(point.attributes or {}) == {
        "capability": "execute_analysis",
        "outcome": "failed",
    }
    assert getattr(found["ra_query_bytes"].data.data_points[0], "value", None) == 2048


@contextmanager
def black_hole() -> Iterator[str]:
    """A TCP endpoint that accepts connections and never answers."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(16)
    held: list[socket.socket] = []
    stop = threading.Event()

    def accept() -> None:
        server.settimeout(0.1)
        while not stop.is_set():
            try:
                connection, _ = server.accept()
            except OSError:
                continue
            held.append(connection)

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.getsockname()[1]}"
    finally:
        stop.set()
        thread.join(2)
        for connection in held:
            connection.close()
        server.close()


def closed_port() -> str:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{sock.getsockname()[1]}"


def emit_many(telemetry: Telemetry, count: int) -> float:
    started = time.monotonic()
    for index in range(count):
        with telemetry.span(Span.TOOL, run_id="run_outage", attributes={"n": index}):
            telemetry.count(Metric.TOOL_CALLS, {Label.OUTCOME: "succeeded"})
            telemetry.observe(Metric.TOOL_SECONDS, 0.01)
    return time.monotonic() - started


@pytest.mark.parametrize("backend", ["refused", "black_hole"])
def test_a_telemetry_outage_neither_breaks_nor_slows_the_caller(backend: str) -> None:
    with black_hole() as hung:
        base = hung if backend == "black_hole" else closed_port()
        sink = OtelSink(
            settings(
                traces_endpoint=f"{base}/v1/traces",
                metrics_endpoint=f"{base}/v1/metrics",
                queue_size=64,
                batch_size=16,
            )
        )
        telemetry = Telemetry(sink)
        # Far more spans than the queue holds: extras are dropped, never awaited.
        elapsed = emit_many(telemetry, 600)
        assert elapsed < 2.0, elapsed
        flushed = time.monotonic()
        telemetry.flush(1.0)
        assert time.monotonic() - flushed < 8.0
        sink.shutdown(1.0)
        assert emit_many(telemetry, 50) < 2.0  # after shutdown: still harmless
