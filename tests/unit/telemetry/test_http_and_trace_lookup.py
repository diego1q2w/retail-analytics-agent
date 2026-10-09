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
