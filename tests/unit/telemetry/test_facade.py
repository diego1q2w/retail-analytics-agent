"""Sanitizing and failure isolation of the telemetry facade (canaries)."""

from __future__ import annotations

import pytest

from retail_analytics.application.contracts.telemetry import Label, Metric, Span
from retail_analytics.application.telemetry import (
    Telemetry,
    sanitize_attributes,
    sanitize_labels,
    trace_id_for,
    use_telemetry,
)
from tests.unit.telemetry.recording import RecordingSink, recording

EMAIL = "maria.canary@example.com"
PHONE = "+34 612 345 678"
GEMINI_KEY = "AIzaSyD-canary-key-0123456789abcdefghij"
OPENAI_KEY = "sk-proj-canarycanarycanary0123456789"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJjYW5hcnkifQ.signaturecanary"
SQL = (
    "SELECT * FROM orders WHERE email = 'maria.canary@example.com' "
    "AND k = @_policy_ref_inner"
)
ROW = "{'user_id': 4711, 'first_name': 'Maria', 'email': 'maria.canary@example.com'}"
REFERENCE = "cus_0123456789abcdef0123456789abcdef"
CANARIES = [EMAIL, "612 345 678", GEMINI_KEY, OPENAI_KEY, JWT, "Maria", REFERENCE]


def test_canaries_never_reach_span_attributes() -> None:
    telemetry, sink = recording()
    with telemetry.span(
        Span.TOOL,
        run_id="run_abc",
        attributes={
            "run_id": "run_abc",
            "detail": f"failed for {EMAIL} call {PHONE}",
            "error_summary": f"bad key {GEMINI_KEY} and {OPENAI_KEY} and {JWT}",
            "note": f"{ROW} {REFERENCE}",
            "prompt": "tell me about Maria",
            "sql": SQL,
            "rows": [1, 2],
            "api_key": OPENAI_KEY,
            "authorization": f"Bearer {JWT}",
            "email": EMAIL,
            "capability_sql_text": SQL,
            "bytes_billed": 10,
        },
    ) as span:
        span.set({"query_text": SQL, "more": f"token {OPENAI_KEY}", "count": 3})
        span.event("something", {"detail": EMAIL, "result_rows": "x"})
    text = sink.everything()
    for canary in CANARIES:
        assert canary not in text, canary
    assert "SELECT" not in text and "@_policy" not in text
    recorded = sink.spans[0].attributes
    assert recorded["run_id"] == "run_abc" and recorded["bytes_billed"] == 10
    assert recorded["count"] == 3
    for denied in ("prompt", "sql", "rows", "api_key", "authorization", "email"):
        assert denied not in recorded


def test_identifier_keys_accept_only_identifiers() -> None:
    clean = sanitize_attributes(
        {
            "run_id": "run_0123abcd",
            "operation_id": EMAIL,
            "job_id": OPENAI_KEY,
            "session_id": "has space",
        }
    )
    assert clean == {"run_id": "run_0123abcd"}


def test_metric_labels_are_bounded_and_never_identifiers() -> None:
    labels = sanitize_labels(
        {
            Label.CAPABILITY: "execute_analysis",
            Label.MODEL: "gemini-3.8-flash",
            Label.REASON: EMAIL,
            Label.PROVIDER: trace_id_for("run_1") + trace_id_for("run_2"),
            Label.ERROR_CODE: OPENAI_KEY,
            Label.ROUTE: "/v1/runs/{run_id}",
        }
    )
    assert labels[Label.CAPABILITY] == "execute_analysis"
    assert labels[Label.MODEL] == "gemini-3.8-flash"
    assert labels[Label.ROUTE] == "/v1/runs/{run_id}"
    assert labels[Label.REASON] == "other"
    assert labels[Label.PROVIDER] == "other"
    assert labels[Label.ERROR_CODE] == "other"


def test_canaries_never_reach_metric_labels() -> None:
    telemetry, sink = recording()
    telemetry.count(Metric.TOOL_CALLS, {Label.REASON: f"{EMAIL}"}, 1)
    telemetry.observe(Metric.TOOL_SECONDS, 0.5, {Label.CAPABILITY: SQL})
    assert EMAIL not in sink.everything() and "SELECT" not in sink.everything()


def test_span_failure_records_the_exception_type_only() -> None:
    telemetry, sink = recording()
    with (
        pytest.raises(ValueError, match="canary"),
        telemetry.span("tool.call", run_id="run_x"),
    ):
        raise ValueError(f"row {ROW} {EMAIL}")
    span = sink.spans[0]
    assert span.failed == "ValueError" and span.ended
    assert EMAIL not in sink.everything()


class ExplodingSink(RecordingSink):
    def span(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        raise RuntimeError("exporter down")

    def count(self, *args: object, **kwargs: object) -> None:
        raise RuntimeError("exporter down")

    def observe(self, *args: object, **kwargs: object) -> None:
        raise RuntimeError("exporter down")

    def flush(self, timeout_seconds: float) -> None:
        raise RuntimeError("exporter down")


def test_a_failing_sink_never_breaks_the_caller() -> None:
    telemetry = Telemetry(ExplodingSink())
    with telemetry.span("tool.call", run_id="run_x", attributes={"a": 1}) as span:
        span.set({"b": 2})
        span.event("e", {})
        span.fail("X")
        result = 41 + 1
    telemetry.count(Metric.TOOL_CALLS, {Label.OUTCOME: "failed"})
    telemetry.observe(Metric.TOOL_SECONDS, 1.0)
    telemetry.flush()
    assert result == 42


def test_the_callers_exceptions_still_propagate_through_a_failing_sink() -> None:
    telemetry = Telemetry(ExplodingSink())
    with pytest.raises(KeyError), telemetry.span("tool.call"):
        raise KeyError("x")


def test_installation_is_scoped_and_default_is_a_no_op() -> None:
    from retail_analytics.application.telemetry import telemetry as current

    assert not current().enabled
    fresh, _ = recording()
    with use_telemetry(fresh):
        assert current().enabled
    assert not current().enabled


def test_trace_ids_are_deterministic_per_run() -> None:
    assert trace_id_for("run_1") == trace_id_for("run_1") != trace_id_for("run_2")
    assert len(trace_id_for("run_1")) == 32


@pytest.mark.real_telemetry
def test_telemetry_is_on_by_default_and_built_only_when_enabled() -> None:
    from retail_analytics.bootstrap.config import BackendSettings
    from retail_analytics.bootstrap.telemetry import build_telemetry

    assert BackendSettings().telemetry_enabled is True
    assert not build_telemetry(BackendSettings(telemetry_enabled=False), "api").enabled
    enabled = build_telemetry(
        BackendSettings(
            telemetry_enabled=True,
            telemetry_traces_endpoint="http://127.0.0.1:1/v1/traces",
            telemetry_metrics_endpoint="http://127.0.0.1:1/v1/metrics",
        ),
        "worker",
    )
    try:
        assert enabled.enabled
    finally:
        enabled.flush(0.5)


def test_the_test_suite_never_builds_a_real_sink_from_default_settings() -> None:
    """Default settings are telemetry-on; conftest keeps tests offline."""
    import os

    from retail_analytics.bootstrap.config import BackendSettings
    from retail_analytics.bootstrap.telemetry import build_telemetry

    assert os.environ["RETAIL_ANALYTICS_TELEMETRY_ENABLED"] == "false"
    assert BackendSettings().telemetry_enabled is True
    assert not build_telemetry(BackendSettings(), "api").enabled
