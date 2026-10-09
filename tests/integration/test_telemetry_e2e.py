"""Traces and metrics of a real durable run reach MLflow, Prometheus, Grafana.

A worker process runs the real Temporal workflow with the Gemini/GPT provider
chain over HTTP stubs: Gemini calls a tool, then is overloaded, and GPT
answers (after a clarification in the conversation test). The run's trace
must show the API acceptance, workflow, tool attempt, model attempts with the
fallback and the answering provider, with the sanitized interaction as span
inputs/outputs; the dashboards must query the metrics; no canary may appear
anywhere; and stopping the telemetry backends must not stop runs.
"""

from __future__ import annotations

import json
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest

from retail_analytics.application.telemetry import (
    install_telemetry,
    telemetry,
    trace_id_for,
)
from retail_analytics.bootstrap.config import BackendSettings, RuntimeMode
from retail_analytics.bootstrap.telemetry import build_telemetry
from retail_analytics.bootstrap.trace_lookup import span_attributes, span_lines
from retail_analytics.domain.runs import RunStatus
from tests.integration.compose_stack import (
    COMPOSE_FILE,
    Stack,
    _free_port,
    _require_docker,
)
from tests.integration.test_investigations import TestEnv

pytestmark = [pytest.mark.docker, pytest.mark.asyncio]
ROOT = Path(__file__).resolve().parents[2]
DASHBOARD = ROOT / "docker" / "grafana" / "dashboards" / "agent-overview.json"
GRAFANA_PASSWORD = "test-grafana-" + uuid.uuid4().hex
MLFLOW_PASSWORD = "test-mlflow-" + uuid.uuid4().hex
CANARY = "canary.person@example.com"
SERVICES = ("postgres", "temporal", "mlflow", "prometheus", "grafana")


@dataclass(frozen=True)
class TelemetryStack(Stack):
    mlflow_port: int = field(default_factory=_free_port)
    prometheus_port: int = field(default_factory=_free_port)
    grafana_port: int = field(default_factory=_free_port)

    @property
    def env(self) -> dict[str, str]:
        return {
            **super().env,
            "COMPOSE_MLFLOW_PORT": str(self.mlflow_port),
            "COMPOSE_PROMETHEUS_PORT": str(self.prometheus_port),
            "COMPOSE_GRAFANA_PORT": str(self.grafana_port),
            "COMPOSE_MLFLOW_DB_PASSWORD": MLFLOW_PASSWORD,
            "COMPOSE_GRAFANA_ADMIN_PASSWORD": GRAFANA_PASSWORD,
        }

    @property
    def traces_endpoint(self) -> str:
        return f"http://127.0.0.1:{self.mlflow_port}/v1/traces"

    @property
    def metrics_endpoint(self) -> str:
        return f"http://127.0.0.1:{self.prometheus_port}/api/v1/otlp/v1/metrics"


@pytest.fixture(scope="module")
def stack() -> Iterator[TelemetryStack]:
    _require_docker()
    stack = TelemetryStack()
    try:
        stack.compose("up", "-d", "--build", "--wait", *SERVICES)
        stack.compose("run", "--rm", "temporal-namespace")
        stack.migrate()
        # The controlled effect tool of the shared test worker writes here.
        with psycopg.connect(stack.app_dsn) as connection:
            connection.execute(
                "CREATE TABLE t13_test_effects ("
                "operation_id text PRIMARY KEY, run_id text NOT NULL)"
            )
        yield stack
    finally:
        subprocess.run(
            [  # noqa: S607
                "docker",
                "compose",
                "-f",
                str(COMPOSE_FILE),
                "-p",
                stack.project,
                "down",
                "--volumes",
                "--remove-orphans",
            ],
            env=stack.env,
            capture_output=True,
            check=False,
            timeout=300,
        )


def eventually[T](what: str, check: Callable[[], T | None], seconds: float = 60) -> T:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            found = check()
            if found:
                return found
        except (httpx.HTTPError, ValueError, KeyError, IndexError):
            pass
        time.sleep(1)
    raise AssertionError(f"timed out waiting for {what}")


def mlflow_spans(stack: TelemetryStack, run_id: str) -> list[dict[str, Any]]:
    response = httpx.get(
        f"http://127.0.0.1:{stack.mlflow_port}/api/3.0/mlflow/traces/batchGet",
        params={"trace_ids": "tr-" + trace_id_for(run_id)},
        timeout=10,
    )
    response.raise_for_status()
    traces = response.json()["traces"]
    spans: list[dict[str, Any]] = traces[0]["spans"] if traces else []
    return spans


def mlflow_trace_info(stack: TelemetryStack, run_id: str) -> dict[str, Any]:
    response = httpx.get(
        f"http://127.0.0.1:{stack.mlflow_port}/api/3.0/mlflow/traces/batchGet",
        params={"trace_ids": "tr-" + trace_id_for(run_id)},
        timeout=10,
    )
    response.raise_for_status()
    info: dict[str, Any] = response.json()["traces"][0]["trace_info"]
    return info


_CONTENT_KEYS = ("mlflow.spanInputs", "mlflow.spanOutputs")


def payload(span: dict[str, Any], side: str) -> Any:
    """A span's captured content as MLflow stores it (JSON)."""
    key = f"mlflow.span{side.capitalize()}"
    raw = span.get("attributes")
    value: Any
    if isinstance(raw, dict):
        value = raw[key]
    else:
        (item,) = [a for a in raw or [] if a.get("key") == key]
        value = _otlp_value(item["value"])
    while isinstance(value, str):
        value = json.loads(value)
    return value


def _otlp_value(value: dict[str, Any]) -> Any:
    """An OTLP ``AnyValue`` (as MLflow's batchGet returns it) as plain data."""
    if "kvlist_value" in value:
        return {
            item["key"]: _otlp_value(item.get("value", {}))
            for item in value["kvlist_value"].get("values", [])
        }
    if "array_value" in value:
        return [_otlp_value(v) for v in value["array_value"].get("values", [])]
    if "int_value" in value:
        return int(value["int_value"])
    for kind in ("string_value", "bool_value", "double_value"):
        if kind in value:
            return value[kind]
    return None


def metadata_of(span: dict[str, Any]) -> dict[str, str]:
    return {k: v for k, v in span_attributes(span).items() if k not in _CONTENT_KEYS}


def prometheus(stack: TelemetryStack, query: str) -> list[dict[str, Any]]:
    response = httpx.get(
        f"http://127.0.0.1:{stack.prometheus_port}/api/v1/query",
        params={"query": query},
        timeout=10,
    )
    response.raise_for_status()
    result: list[dict[str, Any]] = response.json()["data"]["result"]
    return result


@contextmanager
def api_telemetry(stack: TelemetryStack) -> Iterator[None]:
    """This process plays the API: it accepts runs and exports like the API."""
    settings = BackendSettings(
        mode=RuntimeMode.FIXTURE,
        telemetry_enabled=True,
        telemetry_traces_endpoint=stack.traces_endpoint,
        telemetry_metrics_endpoint=stack.metrics_endpoint,
        telemetry_export_timeout_seconds=1.0,
        telemetry_metric_interval_seconds=1.0,
    )
    previous = telemetry()
    installed = build_telemetry(settings, "api")
    install_telemetry(installed)
    try:
        yield
    finally:
        installed.flush(5.0)
        install_telemetry(previous)


async def completed(
    env: TestEnv, process: subprocess.Popen[str], run_id: str, seconds: float = 90
) -> None:
    try:
        await env.wait_status(run_id, RunStatus.COMPLETED, seconds)
    except TimeoutError:
        handle = env.client.get_workflow_handle(env.scheduler.workflow_id(run_id))
        history = await handle.fetch_history()
        failures = [e.event_type for e in history.events][-12:]
        env.stop(process)
        stdout, stderr = process.communicate()
        pytest.fail(
            f"run did not complete; worker said: {stdout[-2000:]} {stderr[-4000:]}"
            f" history failures: {failures[-3:]}"
        )


def worker_with_telemetry(
    env: TestEnv, stack: TelemetryStack, monkeypatch: pytest.MonkeyPatch
) -> subprocess.Popen[str]:
    monkeypatch.setenv("T30_TRACES_ENDPOINT", stack.traces_endpoint)
    monkeypatch.setenv("T30_METRICS_ENDPOINT", stack.metrics_endpoint)
    return env.worker(stack, providers="fallback")


def check_dashboard(stack: TelemetryStack) -> None:
    """Every dashboard query is valid against the live data source."""
    dashboard = json.loads(DASHBOARD.read_text())
    queries = [
        (panel["title"], target["expr"])
        for panel in dashboard["panels"]
        for target in panel.get("targets", [])
    ]
    assert len(queries) >= 30
    base = f"http://127.0.0.1:{stack.grafana_port}"
    for title, expr in queries:
        answer = httpx.get(
            base + "/api/datasources/proxy/uid/prometheus/api/v1/query",
            params={
                "query": expr.replace("$__rate_interval", "1m").replace(
                    "$__range", "1h"
                )
            },
            auth=("admin", GRAFANA_PASSWORD),
            timeout=10,
        )
        assert answer.status_code == 200, (title, answer.text[:200])
        assert answer.json()["status"] == "success", title
    provisioned = httpx.get(
        base + "/api/search",
        params={"query": "Agent overview"},
        auth=("admin", GRAFANA_PASSWORD),
        timeout=10,
    ).json()
    assert "ra-agent-overview" in {item["uid"] for item in provisioned}


async def test_run_with_fallback_is_traced_measured_and_sanitized(
    stack: TelemetryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = await TestEnv.create(stack)
    process = worker_with_telemetry(env, stack, monkeypatch)
    try:
        with api_telemetry(stack):
            run_id = await env.start(f"Analyze sales: effect case. Ask {CANARY}.")
        await completed(env, process, run_id)
        operations = await env.db.tool_executions.for_run(run_id)
        (operation,) = operations

        def complete_trace() -> list[dict[str, Any]] | None:
            spans = mlflow_spans(stack, run_id)
            names = {s["name"] for s in spans}
            needed = {
                "run.accept",
                "run.admission",
                "investigation.run",
                "tool.call",
                "model.attempt",
            }
            return spans if needed <= names else None

        spans = eventually("the run's trace in MLflow", complete_trace, 90)
        by_name: dict[str, list[dict[str, Any]]] = {}
        for span in spans:
            by_name.setdefault(span["name"], []).append(span)

        # One trace for API acceptance, tool attempt, model attempts and the run.
        assert {s["trace_id"] for s in spans} == {spans[0]["trace_id"]}
        (root,) = by_name["investigation.run"]
        assert root.get("parent_span_id") in (None, "")
        assert {
            s["parent_span_id"] for s in by_name["run.accept"] + by_name["tool.call"]
        } == {root["span_id"]}
        # One acyclic tree: every parent is in the trace and leads to the root.
        parents = {s["span_id"]: s.get("parent_span_id") or None for s in spans}
        for span_id in parents:
            seen: set[str] = set()
            while parents[span_id] is not None:
                assert span_id not in seen, "parent cycle"
                seen.add(span_id)
                assert parents[span_id] in parents
                span_id = parents[span_id]
            assert span_id == root["span_id"]
        # The admission decision is diagnosable from codes alone.
        (admission,) = by_name["run.admission"]
        admitted = span_attributes(admission)
        assert admitted["decision"] == "proceed"
        assert admitted["classifier_version"] == "request-scope/3"
        assert admitted["topic"] and admitted["reason"]
        tree = span_lines(spans)
        assert tree[0].startswith("investigation.run")
        assert "decision=proceed" in "\n".join(tree)
        accept = span_attributes(by_name["run.accept"][0])
        assert accept["run_id"] == run_id
        tool = span_attributes(by_name["tool.call"][0])
        assert tool["operation_id"] == operation.operation_id
        assert tool["capability"] == "checkpoint_effect"
        assert tool["outcome"] == "succeeded" and tool["run_id"] == run_id

        # Provider, model, attempt and fallback per model request; who answered.
        attempts = [span_attributes(s) for s in by_name["model.attempt"]]
        failed = [a for a in attempts if a["outcome"] == "failed"]
        assert {a["provider"] for a in failed} == {"google-interactions"}
        assert {a["reason_class"] for a in failed} == {"server_error"}
        assert sorted(int(a["attempt"]) for a in failed) == [1, 2, 3]
        (backup,) = [
            a
            for a in attempts
            if a["provider"] == "openai" and a["outcome"] == "succeeded"
        ]
        assert backup["fallback_from"] == "google-interactions"
        assert backup["fallback_reason"] == "server_error"
        assert backup["model"] and backup["attempt"] == "1"
        run = span_attributes(root)
        assert run["status"] == "completed"
        assert run["answered_by"] == "openai"
        assert run["fallback_from"] == "google-interactions"

        # Estimated model spend (T30-F3): per attempt, matching the run total
        # from accounting and MLflow's own trace total (attempts only).
        assert {a["cost_status"] for a in failed} == {"approximate"}
        assert backup["cost_status"] == "estimated"
        assert backup["price_source"] == "genai-prices"
        assert "openai/gpt-5-mini" in backup["price_version"]
        attempt_costs = [float(a["cost_usd"]) for a in attempts if "cost_usd" in a]
        assert float(backup["cost_usd"]) > 0
        assert float(run["model_cost_usd"]) == pytest.approx(sum(attempt_costs))
        assert run["model_cost_complete"].lower() == "true"
        assert float(run["model_cost_limit_usd"]) == 1.0

        def trace_cost_matches() -> bool:
            metadata = mlflow_trace_info(stack, run_id).get("trace_metadata", {})
            raw = metadata.get("mlflow.trace.cost")
            if raw is None:
                return False
            total = float(json.loads(raw)["total_cost"])
            return total == pytest.approx(sum(attempt_costs))

        eventually("the matching trace cost in MLflow", trace_cost_matches, 60)

        # The same data as metrics, without identifiers.
        def has_metrics() -> bool:
            return bool(
                prometheus(stack, 'ra_runs_total{status="completed"}')
                and prometheus(
                    stack,
                    'ra_final_answers_total{provider="openai",outcome="fallback"}',
                )
                and prometheus(
                    stack,
                    'ra_model_fallbacks_total{from_provider="google-interactions",'
                    'to_provider="openai",reason_class="server_error"}',
                )
                and prometheus(
                    stack, 'ra_tool_calls_total{capability="checkpoint_effect"}'
                )
                and prometheus(
                    stack, 'ra_model_cost_usd_total{provider="openai",kind="reported"}'
                )
                and prometheus(stack, 'ra_run_model_cost_usd_count{outcome="complete"}')
            )

        eventually("run metrics in Prometheus", has_metrics, 90)
        names = prometheus(stack, '{__name__=~"ra_.*"}')
        for series in names:
            for value in series["metric"].values():
                assert run_id not in value and operation.operation_id not in value

        # The interaction is readable in the span inputs/outputs (T30-F2) ...
        assert "Analyze sales" in payload(root, "inputs")["request"]
        (tool_span,) = by_name["tool.call"]
        assert payload(tool_span, "inputs")["arguments"] == {
            "purpose": "controlled test"
        }
        assert "model_visible_result" in payload(tool_span, "outputs")
        backup_span = by_name["model.attempt"][-1]
        assert payload(backup_span, "inputs")["provider"] == "openai"
        assert "Backup continued" in json.dumps(payload(backup_span, "outputs"))
        # ... but never in attributes or metric labels, and nothing sensitive
        # anywhere: spans, metric labels or the container logs.
        metadata = json.dumps([metadata_of(s) for s in spans]) + json.dumps(names)
        assert "Backup continued" not in metadata
        assert "Analyze sales" not in metadata
        everything = json.dumps(spans) + json.dumps(names)
        assert CANARY not in everything
        for forbidden in (
            "OPENAI_KEY",
            "sk-openai",
            "gemini-test-key",
            "thought-sig",
            "call-sig",
        ):
            assert forbidden not in everything
        logs = stack.compose("logs", "mlflow", "prometheus").stdout
        assert CANARY not in logs

        check_dashboard(stack)
    finally:
        env.stop(process)
        env.db.close()


async def test_conversation_with_clarification_is_readable_and_sanitized(
    stack: TelemetryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clarification, reply, tool call, retries, fallback and the released
    answer, in order, as sanitized span inputs/outputs of one trace."""
    env = await TestEnv.create(stack)
    process = worker_with_telemetry(env, stack, monkeypatch)
    # Built at runtime: no key-shaped literal in source.
    key = "sk-" + "canary" * 5 + "0123456789"
    try:
        with api_telemetry(stack):
            run_id = await env.start(
                f"Analyze sales: effect case, clarification case. Ask {CANARY}."
            )
            await env.wait_status(run_id, RunStatus.WAITING_FOR_INPUT, 90)
            attached = await env.services.control.attach(env.principal, run_id=run_id)
            assert attached.open_question_id is not None
            await env.services.control.answer(
                env.principal,
                run_id=run_id,
                question_id=attached.open_question_id,
                text=f"Use last full month sales. My key is {key}",
                submission_key="answer",
            )
        await completed(env, process, run_id)

        def complete_trace() -> list[dict[str, Any]] | None:
            spans = mlflow_spans(stack, run_id)
            names = {s["name"] for s in spans}
            needed = {
                "investigation.run",
                "clarification.ask",
                "user.input",
                "answer.release",
                "tool.call",
            }
            return spans if needed <= names else None

        spans = eventually("the conversation trace in MLflow", complete_trace, 90)
        spans.sort(key=lambda s: int(s["start_time_unix_nano"]))
        order = [
            s["name"]
            for s in spans
            if s["name"]
            in ("run.accept", "clarification.ask", "user.input", "answer.release")
        ]
        assert order == [
            "run.accept",
            "clarification.ask",
            "user.input",
            "answer.release",
        ]
        by_name: dict[str, list[dict[str, Any]]] = {}
        for span in spans:
            by_name.setdefault(span["name"], []).append(span)

        (question,) = by_name["clarification.ask"]
        assert payload(question, "inputs")["model_draft"].startswith("Which period?")
        released_question = payload(question, "outputs")["released_question"]
        assert released_question.startswith("Which period?")
        (reply,) = by_name["user.input"]
        assert payload(reply, "inputs")["kind"] == "answer"
        assert "Use last full month" in payload(reply, "inputs")["text"]
        (release,) = by_name["answer.release"]
        assert payload(release, "inputs")["model_draft"].startswith("Backup continued")
        assert payload(release, "outputs")["outcome"] == "released"
        assert payload(release, "outputs")["released_answer"].startswith(
            "Backup continued"
        )
        attempts = by_name["model.attempt"]
        failed = [a for a in attempts if "error" in payload(a, "outputs")]
        assert failed and all(
            payload(a, "outputs")["error"]["status_code"] == 503 for a in failed
        )
        for attempt in attempts:
            sent = payload(attempt, "inputs")
            assert sent["messages"][0]["role"] == "system"
            assert metadata_of(attempt)["capture.inputs.chars"]
        (root,) = by_name["investigation.run"]
        assert "user's answer" in payload(root, "inputs")["request"]
        assert payload(root, "outputs")["status"] == "completed"

        everything = json.dumps(spans)
        for forbidden in (CANARY, "canary@example.com", key, "thought-sig"):
            assert forbidden not in everything
        assert "[withheld]" in everything and "[redacted]" in everything

        # The trace list shows the request and answer previews.
        assert "Analyze sales" in json.dumps(mlflow_trace_info(stack, run_id))
    finally:
        env.stop(process)
        env.db.close()


async def test_a_telemetry_outage_does_not_break_or_stall_runs(
    stack: TelemetryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = await TestEnv.create(stack)
    process = worker_with_telemetry(env, stack, monkeypatch)
    try:
        stack.compose("stop", "mlflow", "prometheus")
        started = time.monotonic()
        with api_telemetry(stack):
            run_id = await env.start("Analyze sales: effect case.")
        await completed(env, process, run_id)
        assert process.poll() is None  # the worker never crashed
        assert time.monotonic() - started < 60
        # Still serving: a second run completes while the backends stay down.
        second = await env.start("Analyze sales: effect case, again.")
        await completed(env, process, second)
        # The run records (audit) are unaffected: persisted in PostgreSQL.
        assert (await env.db.runs.get_run(run_id)) is not None
    finally:
        env.stop(process)
        env.db.close()
