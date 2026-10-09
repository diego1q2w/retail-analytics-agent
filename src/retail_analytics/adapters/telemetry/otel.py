"""OpenTelemetry sink: traces to MLflow, metrics to Prometheus (OTLP/HTTP).

Both backends are best effort and can never slow or break a run:

- spans go through a ``BatchSpanProcessor`` (bounded queue; a full queue drops
  new spans) and metrics through a periodic reader; both export on background
  threads, so starting or ending a span only touches memory;
- every export has a short timeout, and after a failure the exporter skips
  sends for a cool-off period (circuit breaker) instead of retrying a dead
  endpoint with every batch; skipped and failed batches are dropped and
  counted (``ra_telemetry_dropped_total``);
- exporter errors are swallowed and reported as a failed export only.

Correlation: all spans of a run share the trace id derived from the run id
(``application.telemetry.trace_id_for``). A process that has no active span for
the run parents its spans on the run's deterministic root span id; the root
span itself is emitted when the run closes (``root=True``) with that id.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.metrics import CallbackOptions, Counter, Histogram, Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    MetricExportResult,
    MetricReader,
    MetricsData,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.sdk.trace.id_generator import IdGenerator, RandomIdGenerator
from opentelemetry.trace import (
    NonRecordingSpan,
    SpanContext,
    Status,
    StatusCode,
    TraceFlags,
)

from retail_analytics.application.contracts.telemetry import (
    HISTOGRAMS,
    Attributes,
    Label,
    Metric,
    Span,
)
from retail_analytics.application.ports.telemetry import SpanHandle
from retail_analytics.application.telemetry import root_span_id_for, trace_id_for

_SECONDS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 45, 90, 180, 360, 720)
_RATIO = (0.1, 0.25, 0.5, 0.75, 0.9, 1.0)
_SPAN_TYPES = {
    Span.RUN: "AGENT",
    Span.TOOL: "TOOL",
    Span.MODEL_REQUEST: "LLM",
    Span.MODEL_ATTEMPT: "LLM",
    Span.RETRIEVAL: "RETRIEVER",
}


@dataclass(frozen=True, slots=True)
class OtelSettings:
    service: str
    instance: str
    traces_endpoint: str
    metrics_endpoint: str
    experiment_id: str = "0"
    export_timeout_seconds: float = 2.0
    metric_interval_seconds: float = 10.0
    queue_size: int = 2048
    batch_size: int = 256
    trace_delay_seconds: float = 1.0
    cooloff_seconds: float = 15.0


class _Breaker:
    """Skip sends for a while after one fails."""

    def __init__(self, cooloff: float, clock: Callable[[], float]) -> None:
        self._cooloff = cooloff
        self._clock = clock
        self._until = 0.0

    def open(self) -> bool:
        return self._clock() < self._until

    def tripped(self) -> None:
        self._until = self._clock() + self._cooloff


class DropCounter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts = {"traces": 0, "metrics": 0}

    def add(self, kind: str, amount: int = 1) -> None:
        with self._lock:
            self._counts[kind] += amount

    def totals(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)


class _GuardedSpanExporter(OTLPSpanExporter):
    def __init__(
        self,
        breaker: _Breaker,
        drops: DropCounter,
        *,
        endpoint: str,
        headers: dict[str, str],
        timeout: float,
    ) -> None:
        super().__init__(endpoint=endpoint, headers=headers, timeout=timeout)
        self._breaker = breaker
        self._drops = drops

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        if self._breaker.open():
            self._drops.add("traces", len(spans))
            return SpanExportResult.FAILURE
        try:
            result = super().export(spans)
        except Exception:
            result = SpanExportResult.FAILURE
        if result is not SpanExportResult.SUCCESS:
            self._breaker.tripped()
            self._drops.add("traces", len(spans))
        return result


class _GuardedMetricExporter(OTLPMetricExporter):
    def __init__(
        self, breaker: _Breaker, drops: DropCounter, *, endpoint: str, timeout: float
    ) -> None:
        super().__init__(endpoint=endpoint, timeout=timeout)
        self._breaker = breaker
        self._drops = drops

    def export(
        self,
        metrics_data: MetricsData,
        timeout_millis: float | None = 10_000,
        **kwargs: object,
    ) -> MetricExportResult:
        if self._breaker.open():
            self._drops.add("metrics")
            return MetricExportResult.FAILURE
        try:
            result = super().export(metrics_data, timeout_millis, **kwargs)
        except Exception:
            result = MetricExportResult.FAILURE
        if result is not MetricExportResult.SUCCESS:
            self._breaker.tripped()
            self._drops.add("metrics")
        return result


_forced_ids: ContextVar[tuple[int, int] | None] = ContextVar("forced_ids", default=None)


class _Ids(IdGenerator):
    """Random ids, except for a run's root span, whose ids are derived."""

    def __init__(self) -> None:
        self._random = RandomIdGenerator()

    def generate_span_id(self) -> int:
        forced = _forced_ids.get()
        return forced[1] if forced else self._random.generate_span_id()

    def generate_trace_id(self) -> int:
        forced = _forced_ids.get()
        return forced[0] if forced else self._random.generate_trace_id()


class _Handle:
    def __init__(self, span: trace.Span) -> None:
        self._span = span

    def set(self, attributes: Attributes) -> None:
        for key, value in attributes.items():
            self._span.set_attribute(key, value)

    def event(self, name: str, attributes: Attributes) -> None:
        self._span.add_event(name, attributes)

    def fail(self, error_type: str) -> None:
        self._span.set_attribute("error.type", error_type)
        self._span.set_status(Status(StatusCode.ERROR, error_type))


def _ns(moment: datetime) -> int:
    return int(moment.timestamp() * 1_000_000_000)


def instrument_names(metric: Metric) -> tuple[str, str]:
    """(OpenTelemetry instrument name, unit). Prometheus adds the suffixes."""
    name = metric.value
    if metric in HISTOGRAMS:
        if name.endswith("_seconds"):
            return name.removesuffix("_seconds"), "s"
        return name, "1"
    return name.removesuffix("_total"), "1"


class OtelSink:
    """Implements ``TelemetrySink`` with the OpenTelemetry SDK."""

    def __init__(
        self,
        settings: OtelSettings,
        *,
        span_exporter: SpanExporter | None = None,
        metric_reader: MetricReader | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._drops = DropCounter()
        breaker = _Breaker(settings.cooloff_seconds, clock)
        resource = Resource.create(
            {"service.name": settings.service, "service.instance.id": settings.instance}
        )
        exporter = span_exporter or _GuardedSpanExporter(
            breaker,
            self._drops,
            endpoint=settings.traces_endpoint,
            headers={"x-mlflow-experiment-id": settings.experiment_id},
            timeout=int(max(settings.export_timeout_seconds, 1)),
        )
        self._traces = TracerProvider(resource=resource, id_generator=_Ids())
        self._traces.add_span_processor(
            BatchSpanProcessor(
                exporter,
                max_queue_size=settings.queue_size,
                max_export_batch_size=settings.batch_size,
                schedule_delay_millis=int(settings.trace_delay_seconds * 1000),
                export_timeout_millis=int(settings.export_timeout_seconds * 1000),
            )
        )
        self._tracer = self._traces.get_tracer("retail_analytics")
        reader = metric_reader or PeriodicExportingMetricReader(
            _GuardedMetricExporter(
                _Breaker(settings.cooloff_seconds, clock),
                self._drops,
                endpoint=settings.metrics_endpoint,
                timeout=int(max(settings.export_timeout_seconds, 1)),
            ),
            export_interval_millis=int(settings.metric_interval_seconds * 1000),
            export_timeout_millis=int(settings.export_timeout_seconds * 1000),
        )
        views = [
            View(
                instrument_name=instrument_names(metric)[0],
                aggregation=ExplicitBucketHistogramAggregation(
                    _RATIO if metric is Metric.RUN_BUDGET_USE else _SECONDS
                ),
            )
            for metric in HISTOGRAMS
        ]
        self._metrics = MeterProvider(
            resource=resource, metric_readers=[reader], views=views
        )
        self._meter = self._metrics.get_meter("retail_analytics")
        self._counters: dict[Metric, Counter] = {}
        self._histograms: dict[Metric, Histogram] = {}
        self._lock = threading.Lock()
        name, unit = instrument_names(Metric.TELEMETRY_DROPPED)
        self._meter.create_observable_counter(
            name, callbacks=[self._observe_drops], unit=unit
        )

    # -- spans ----------------------------------------------------------

    @contextmanager
    def span(
        self,
        name: str,
        *,
        run_id: str | None,
        attributes: Attributes,
        start: datetime | None = None,
        root: bool = False,
    ) -> Iterator[SpanHandle]:
        attrs: dict[str, str | int | float | bool] = dict(attributes)
        kind = _SPAN_TYPES.get(Span(name)) if name in Span else None
        attrs["mlflow.spanType"] = json.dumps(kind or "CHAIN")
        if run_id is not None:
            attrs["run_trace_id"] = trace_id_for(run_id)
        start_ns = None if start is None else _ns(start)
        token = None
        parent_context = None
        forced = None
        if run_id is not None:
            trace_int = int(trace_id_for(run_id), 16)
            if root:
                forced = (trace_int, int(root_span_id_for(run_id), 16))
            else:
                current = trace.get_current_span().get_span_context()
                if not (current.is_valid and current.trace_id == trace_int):
                    parent = SpanContext(
                        trace_int,
                        int(root_span_id_for(run_id), 16),
                        is_remote=True,
                        trace_flags=TraceFlags(TraceFlags.SAMPLED),
                    )
                    parent_context = trace.set_span_in_context(NonRecordingSpan(parent))
        ids_token = _forced_ids.set(forced) if forced else None
        try:
            span = self._tracer.start_span(
                name,
                context=parent_context,
                attributes=attrs,
                start_time=start_ns,
            )
        finally:
            if ids_token is not None:
                _forced_ids.reset(ids_token)
        ctx = trace.set_span_in_context(span, parent_context)
        token = otel_context.attach(ctx)
        try:
            yield _Handle(span)
        finally:
            otel_context.detach(token)
            span.end()

    # -- metrics --------------------------------------------------------

    def count(self, metric: Metric, value: float, labels: dict[Label, str]) -> None:
        counter = self._counters.get(metric)
        if counter is None:
            with self._lock:
                name, unit = instrument_names(metric)
                counter = self._counters.setdefault(
                    metric, self._meter.create_counter(name, unit=unit)
                )
        counter.add(value, {label.value: text for label, text in labels.items()})

    def observe(self, metric: Metric, value: float, labels: dict[Label, str]) -> None:
        histogram = self._histograms.get(metric)
        if histogram is None:
            with self._lock:
                name, unit = instrument_names(metric)
                histogram = self._histograms.setdefault(
                    metric, self._meter.create_histogram(name, unit=unit)
                )
        histogram.record(value, {label.value: text for label, text in labels.items()})

    def flush(self, timeout_seconds: float) -> None:
        millis = int(timeout_seconds * 1000)
        self._traces.force_flush(millis)
        self._metrics.force_flush(millis)

    def shutdown(self, timeout_seconds: float = 2.0) -> None:
        try:
            self.flush(timeout_seconds)
        finally:
            self._traces.shutdown()
            self._metrics.shutdown(timeout_millis=int(timeout_seconds * 1000))

    def _observe_drops(self, options: CallbackOptions) -> Iterable[Observation]:
        return [
            Observation(total, {Label.KIND.value: kind})
            for kind, total in self._drops.totals().items()
        ]
