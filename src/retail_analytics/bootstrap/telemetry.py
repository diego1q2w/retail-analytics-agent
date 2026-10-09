"""Composition of the telemetry sink (traces to MLflow, metrics to Prometheus)."""

from __future__ import annotations

import atexit
import logging
import os
import socket
import uuid

from retail_analytics.adapters.telemetry.otel import OtelSettings, OtelSink
from retail_analytics.application.telemetry import Telemetry, install_telemetry
from retail_analytics.bootstrap.config import BackendSettings


def build_telemetry(settings: BackendSettings, service: str) -> Telemetry:
    """The sink for ``service`` ("api" or "worker"), or a no-op when disabled."""
    if not settings.telemetry_enabled:
        return Telemetry()
    # Export failures are counted (ra_telemetry_dropped_total); the exporter's
    # own tracebacks would only repeat on every cool-off.
    logging.getLogger("opentelemetry").setLevel(logging.CRITICAL)
    instance = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    sink = OtelSink(
        OtelSettings(
            service=f"retail-analytics-{service}",
            instance=instance,
            traces_endpoint=settings.telemetry_traces_endpoint,
            metrics_endpoint=settings.telemetry_metrics_endpoint,
            experiment_id=settings.telemetry_experiment_id,
            export_timeout_seconds=settings.telemetry_export_timeout_seconds,
            metric_interval_seconds=settings.telemetry_metric_interval_seconds,
        )
    )
    return Telemetry(sink, capture_content=settings.telemetry_capture_content)


def install_from_settings(settings: BackendSettings, service: str) -> Telemetry:
    """Install the process-wide telemetry once; flushed (bounded) at exit."""
    built = build_telemetry(settings, service)
    install_telemetry(built)
    if built.enabled:
        atexit.register(built.flush, settings.telemetry_export_timeout_seconds)
    return built
