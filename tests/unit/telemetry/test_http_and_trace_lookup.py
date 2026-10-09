"""HTTP spans/metrics carry route templates only; trace lookup output."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from retail_analytics.application.contracts.telemetry import Label, Metric, Span
from retail_analytics.application.telemetry import trace_id_for, use_telemetry
from retail_analytics.bootstrap.trace_lookup import (
    span_attributes,
    span_lines,
    trace_ref,
)
from retail_analytics.interfaces.http.telemetry import TelemetryMiddleware
from tests.unit.telemetry.recording import recording

RUN = "run_0123456789abcdef0123456789abcdef"
CANARY = "canary.person@example.com"


def app() -> FastAPI:
    api = FastAPI()
    api.add_middleware(TelemetryMiddleware)

    @api.get("/v1/runs/{run_id}")
    def run(run_id: str) -> dict[str, str]:
        if run_id == "boom":
            raise HTTPException(status_code=503, detail="unavailable")
        return {"run_id": run_id}

    return api


def test_requests_are_labelled_by_route_template_and_status_class() -> None:
    telemetry, sink = recording()
    client = TestClient(app(), raise_server_exceptions=False)
    with use_telemetry(telemetry):
        assert client.get(f"/v1/runs/{RUN}?email={CANARY}").status_code == 200
        assert client.get("/v1/runs/boom").status_code == 503
        assert client.get("/nope").status_code == 404
    assert (
        sink.total(Metric.HTTP_REQUESTS, route="/v1/runs/{run_id}", status="2xx") == 1
    )
    assert (
        sink.total(Metric.HTTP_REQUESTS, route="/v1/runs/{run_id}", status="5xx") == 1
    )
    assert sink.total(Metric.HTTP_REQUESTS, route="unmatched", status="4xx") == 1
    labels = {value for _, _, labels in sink.counts for value in labels.values()}
    assert RUN not in labels and "boom" not in labels
    assert all(set(labels) <= set(Label) for _, _, labels in sink.counts)
    ok, failed, _ = sink.named(Span.HTTP)
    assert ok.attributes["run_id"] == RUN
    assert ok.attributes["run_trace_id"] == trace_id_for(RUN)
    assert ok.attributes["route"] == "/v1/runs/:run_id"
    assert failed.failed == "http_503"
    assert CANARY not in sink.everything()


def test_trace_reference_matches_the_exported_trace_id() -> None:
    assert trace_ref(RUN) == "tr-" + trace_id_for(RUN)


def test_span_tree_reads_both_mlflow_response_shapes() -> None:
    listed = {
        "span_id": "b",
        "parent_span_id": "a",
        "name": "tool.call",
        "start_time_unix_nano": 2,
        "attributes": [
            {"key": "capability", "value": {"string_value": "execute_analysis"}},
            {"key": "error_code", "value": {"string_value": "invalid_query"}},
            {"key": "secret_looking", "value": {"string_value": "ignored"}},
        ],
    }
    encoded = {
        "span_id": "a",
        "parent_span_id": None,
        "name": "investigation.run",
        "start_time_unix_nano": 1,
        "attributes": {"status": '"completed"'},
    }
    assert span_attributes(listed)["capability"] == "execute_analysis"
    assert span_attributes(encoded) == {"status": "completed"}
    assert span_lines([listed, encoded]) == [
        "investigation.run  status=completed",
        "  tool.call  capability=execute_analysis error_code=invalid_query",
    ]


def _span(
    span_id: str, parent: str | None, name: str, start: int, **attributes: str
) -> dict[str, object]:
    return {
        "span_id": span_id,
        "parent_span_id": parent,
        "name": name,
        "start_time_unix_nano": start,
        "attributes": {k: f'"{v}"' for k, v in attributes.items()},
    }


def test_span_tree_lists_every_span_of_a_cyclic_legacy_trace_with_details() -> None:
    # The recorded defect: run root and accept span parent each other.
    spans = [
        _span(
            "root",
            "acc",
            "investigation.run",
            9,
            status="completed",
            answered_by="google-interactions",
        ),
        _span("acc", "root", "run.accept", 1, status="running"),
        _span(
            "tool",
            "acc",
            "tool.call",
            3,
            capability="list_relations",
            outcome="succeeded",
        ),
        _span(
            "model",
            "acc",
            "model.attempt",
            2,
            provider="google-interactions",
            outcome="succeeded",
            prompt="never shown",
        ),
        _span("self", "self", "query.execute", 4, outcome="succeeded"),
    ]
    lines = span_lines(spans)
    assert lines[0].startswith("warning: malformed span hierarchy: 5 span(s)")
    assert "note: the trace has no root span." in lines
    body = [line for line in lines if not line.startswith(("warning", "note"))]
    assert len(body) == 5  # nothing dropped, nothing repeated
    text = "\n".join(lines)
    assert "answered_by=google-interactions" in text
    assert "capability=list_relations outcome=succeeded" in text
    assert "provider=google-interactions" in text
    assert "never shown" not in text
    assert "[in parent cycle]" in text


def test_span_tree_keeps_spans_whose_parent_is_missing() -> None:
    spans = [
        _span(
            "a",
            "unexported",
            "run.admission",
            1,
            decision="proceed",
            topic="data_discovery",
            reason="discovery_pattern",
            classifier_version="request-scope/2",
        ),
        _span("b", "a", "tool.call", 2, capability="describe_relation"),
    ]
    lines = span_lines(spans)
    assert lines[0].startswith("note: 1 span(s) have a parent missing")
    assert lines[2] == (
        "run.admission  [parent missing] reason=discovery_pattern "
        "decision=proceed topic=data_discovery classifier_version=request-scope/2"
    )
    assert lines[3] == "  tool.call  capability=describe_relation"


def test_span_tree_does_not_recurse_on_deep_traces() -> None:
    spans = [_span("s0", None, "investigation.run", 0)] + [
        _span(f"s{i}", f"s{i - 1}", "tool.call", i) for i in range(1, 3000)
    ]
    lines = span_lines(spans)
    assert len(lines) == 3000 and lines[0] == "investigation.run"


def test_admission_diagnostics_survive_sanitizing() -> None:
    from retail_analytics.application.telemetry import sanitize_attributes

    clean = sanitize_attributes(
        {
            "decision": "clarify",
            "topic": "unclear",
            "reason": "no_analysis_terms",
            "classifier_version": "request-scope/2",
        }
    )
    assert clean == {
        "decision": "clarify",
        "topic": "unclear",
        "reason": "no_analysis_terms",
        "classifier_version": "request-scope/2",
    }
