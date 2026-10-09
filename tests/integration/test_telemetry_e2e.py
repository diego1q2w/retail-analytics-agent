"""Traces and metrics of a real durable run reach MLflow, Prometheus, Grafana.

A worker process runs the real Temporal workflow with the Gemini/GPT provider
chain over HTTP stubs: Gemini calls a tool, then is overloaded, and GPT
answers. The run's trace must show the API acceptance, workflow, tool attempt,
model attempts with the fallback and the answering provider; the dashboards
must query the metrics; no canary may appear anywhere; and stopping the
telemetry backends must not stop runs.
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
from retail_analytics.bootstrap.trace_lookup import span_attributes
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
            needed = {"run.accept", "investigation.run", "tool.call", "model.attempt"}
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
            )

        eventually("run metrics in Prometheus", has_metrics, 90)
        names = prometheus(stack, '{__name__=~"ra_.*"}')
        for series in names:
            for value in series["metric"].values():
                assert run_id not in value and operation.operation_id not in value

        # Nothing sensitive anywhere: spans, metric labels or the container logs.
        everything = json.dumps(spans) + json.dumps(names)
        assert CANARY not in everything
        assert "Backup continued" not in everything
        assert "Analyze sales" not in everything
        for forbidden in ("OPENAI_KEY", "sk-openai", "gemini-test-key"):
            assert forbidden not in everything
        logs = stack.compose("logs", "mlflow", "prometheus").stdout
        assert CANARY not in logs

        check_dashboard(stack)
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
