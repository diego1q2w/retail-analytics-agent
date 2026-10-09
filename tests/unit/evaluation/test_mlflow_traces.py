"""Live-smoke trace reading: attributes, payload visibility, SQL, counts."""

from __future__ import annotations

import json
from typing import Any

from retail_analytics.adapters.evaluation.mlflow_traces import (
    span_fields,
    trace_facts,
)


def otlp(**values: Any) -> list[dict[str, Any]]:
    def wrap(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return {
                "kvlist_value": {
                    "values": [{"key": k, "value": wrap(v)} for k, v in value.items()]
                }
            }
        if isinstance(value, int):
            return {"int_value": value}
        return {"string_value": value}

    return [{"key": k, "value": wrap(v)} for k, v in values.items()]


def span(name: str, start: int, **attributes: Any) -> dict[str, Any]:
    return {
        "name": name,
        "start_time_unix_nano": start,
        "attributes": otlp(**attributes),
    }


SPANS = [
    span(
        "query.compile",
        3,
        outcome="compiled",
        **{
            "mlflow.spanInputs": {"generated_sql": "SELECT SUM(x) FROM s"},
            "mlflow.spanOutputs": {"executed_sql": "SELECT SUM(`x`) FROM (…)"},
        },
    ),
    span(
        "model.attempt",
        1,
        provider="google-interactions",
        model="m",
        outcome="succeeded",
        input_tokens=100,
        output_tokens=10,
        **{"mlflow.spanInputs": {"messages": "sanitized"}},
    ),
    span("model.attempt", 2, provider="openai", model="g", outcome="failed"),
    span(
        "tool.call",
        2,
        capability="execute_analysis",
        **{"mlflow.spanInputs": {"sql": "SELECT 1"}},
    ),
    span("query.execute", 4, job_id="ra_1", outcome="succeeded", bytes_billed="20"),
    span("investigation.context_restart", 5, **{"restart.cause": "tamper"}),
]


def test_trace_facts_count_attempts_tools_jobs_and_visibility() -> None:
    facts = trace_facts(SPANS)
    assert facts["model_requests"] == 2
    assert (facts["input_tokens"], facts["output_tokens"]) == (100, 10)
    assert [a["outcome"] for a in facts["attempts"]] == ["succeeded", "failed"]
    assert facts["tools"] == ["execute_analysis"] and facts["queries"] == 1
    assert facts["jobs"][0]["job_id"] == "ra_1"
    assert facts["restarts"] == ["tamper"]
    assert facts["content_visible"] == {
        "prompt": True,
        "tool_arguments": True,
        "sql": True,
    }
    assert "SELECT SUM(x) FROM s" in facts["sql"][0]
    assert "-- executed" in facts["sql"][0]


def test_missing_payloads_are_reported_as_not_visible() -> None:
    bare = [span("model.attempt", 1, outcome="succeeded"), span("tool.call", 2)]
    facts = trace_facts(bare)
    assert facts["content_visible"] == {
        "prompt": False,
        "tool_arguments": False,
        "sql": False,
    }


def test_json_string_attribute_shape() -> None:
    attrs, payloads = span_fields(
        {
            "attributes": {
                "provider": json.dumps("openai"),
                "mlflow.spanInputs": json.dumps({"generated_sql": "SELECT 1"}),
            }
        }
    )
    assert attrs == {"provider": "openai"}
    assert payloads == {"inputs": {"generated_sql": "SELECT 1"}}
