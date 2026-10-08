"""Local telemetry stack checks (MLflow, Prometheus, Grafana).

Need Docker: ``pytest -m docker``.

Each run starts its own Compose project on free loopback ports and removes only
that project's containers, network and volumes afterwards.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest

pytestmark = pytest.mark.docker

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "compose.yaml"
SERVICES = ("postgres", "mlflow", "prometheus", "grafana")
APP_PASSWORD = "test-app-" + uuid.uuid4().hex
MLFLOW_PASSWORD = "test-mlflow-" + uuid.uuid4().hex
ADMIN_PASSWORD = "test-admin-" + uuid.uuid4().hex
GRAFANA_PASSWORD = "test-grafana-" + uuid.uuid4().hex
METRIC = "retail_analytics_synthetic_smoke"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass(frozen=True)
class Stack:
    project: str
    ports: dict[str, int]

    @property
    def env(self) -> dict[str, str]:
        return {
            **os.environ,
            "COMPOSE_POSTGRES_PORT": str(self.ports["postgres"]),
            "COMPOSE_MLFLOW_PORT": str(self.ports["mlflow"]),
            "COMPOSE_PROMETHEUS_PORT": str(self.ports["prometheus"]),
            "COMPOSE_GRAFANA_PORT": str(self.ports["grafana"]),
            "COMPOSE_APP_DB_PASSWORD": APP_PASSWORD,
            "COMPOSE_MLFLOW_DB_PASSWORD": MLFLOW_PASSWORD,
            "COMPOSE_PG_ADMIN_PASSWORD": ADMIN_PASSWORD,
            "COMPOSE_GRAFANA_ADMIN_PASSWORD": GRAFANA_PASSWORD,
        }

    def compose(
        self, *args: str, timeout: int = 600
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["docker", "compose", "-f", str(COMPOSE_FILE), "-p", self.project, *args],  # noqa: S607
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
        if result.returncode != 0:
            raise RuntimeError(f"docker compose {args} failed:\n{result.stderr}")
        return result

    def up(self) -> None:
        self.compose("up", "-d", "--build", "--wait", *SERVICES)

    def url(self, service: str, path: str = "") -> str:
        return f"http://127.0.0.1:{self.ports[service]}{path}"

    def dsn(self, user: str, password: str, database: str) -> str:
        return (
            f"host=127.0.0.1 port={self.ports['postgres']} user={user} "
            f"password={password} dbname={database} connect_timeout=5"
        )


@pytest.fixture(scope="module")
def stack() -> Iterator[Stack]:
    if shutil.which("docker") is None:
        pytest.skip("docker is not installed")
    probe = subprocess.run(
        ["docker", "info"],  # noqa: S607
        capture_output=True,
        check=False,
        timeout=30,
    )
    if probe.returncode != 0:
        pytest.skip("docker daemon is not reachable")
    stack = Stack(
        project="ra-test-" + uuid.uuid4().hex[:8],
        ports={name: _free_port() for name in SERVICES},
    )
    try:
        stack.up()
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


def _eventually(check: Callable[[], bool], what: str, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except (httpx.HTTPError, ValueError, KeyError):
            pass
        time.sleep(1)
    raise AssertionError(f"timed out waiting for {what}")


def _metric_values(stack: Stack, base: str = "prometheus") -> list[str]:
    if base == "prometheus":
        response = httpx.get(
            stack.url("prometheus", "/api/v1/query"), params={"query": METRIC}
        )
    else:
        response = httpx.get(
            stack.url("grafana", "/api/datasources/proxy/uid/prometheus/api/v1/query"),
            params={"query": METRIC},
            auth=("admin", GRAFANA_PASSWORD),
        )
    response.raise_for_status()
    return [item["value"][1] for item in response.json()["data"]["result"]]


def _push_synthetic_metric(stack: Stack) -> None:
    payload = {
        "resourceMetrics": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "t29-smoke"}}
                    ]
                },
                "scopeMetrics": [
                    {
                        "metrics": [
                            {
                                "name": METRIC,
                                "gauge": {
                                    "dataPoints": [
                                        {
                                            "timeUnixNano": str(time.time_ns()),
                                            "asDouble": 42,
                                        }
                                    ]
                                },
                            }
                        ]
                    }
                ],
            }
        ]
    }
    httpx.post(
        stack.url("prometheus", "/api/v1/otlp/v1/metrics"), json=payload
    ).raise_for_status()


def _create_sample_trace(stack: Stack) -> str:
    base = stack.url("mlflow", "/api/2.0/mlflow/experiments/create")
    created = httpx.post(base, json={"name": "t29-smoke-" + uuid.uuid4().hex[:6]})
    created.raise_for_status()
    experiment_id = created.json()["experiment_id"]
    trace_id = "tr-t29" + uuid.uuid4().hex[:16]
    # Sanitized sample: timing, outcome and correlation only; no prompts or rows.
    body: dict[str, Any] = {
        "trace": {
            "trace_info": {
                "trace_id": trace_id,
                "trace_location": {
                    "type": "MLFLOW_EXPERIMENT",
                    "mlflow_experiment": {"experiment_id": experiment_id},
                },
                "request_time": "2026-10-08T09:00:00Z",
                "execution_duration": "0.250s",
                "state": "OK",
                "trace_metadata": {"smoke": "t29"},
                "tags": {"mlflow.traceName": "synthetic-smoke"},
            }
        }
    }
    httpx.post(
        stack.url("mlflow", "/api/3.0/mlflow/traces"), json=body
    ).raise_for_status()
    return trace_id


def _trace_state(stack: Stack, trace_id: str) -> str:
    response = httpx.get(stack.url("mlflow", f"/api/3.0/mlflow/traces/{trace_id}"))
    response.raise_for_status()
    return str(response.json()["trace"]["trace_info"]["state"])


def test_services_are_healthy_and_loopback_only(stack: Stack) -> None:
    out = stack.compose("ps", "--format", "json", "--all")
    rows = [json.loads(line) for line in out.stdout.splitlines() if line.strip()]
    health = {row["Service"]: row.get("Health", "") for row in rows}
    for name in SERVICES:
        assert health[name] == "healthy", health
    for row in rows:
        for publisher in row.get("Publishers") or []:
            if publisher.get("PublishedPort"):
                assert publisher["URL"] == "127.0.0.1", row["Service"]


def test_mlflow_database_is_isolated_from_other_roles(stack: Stack) -> None:
    mlflow = stack.dsn("mlflow", MLFLOW_PASSWORD, "mlflow")
    with psycopg.connect(mlflow) as conn:
        row = conn.execute(
            "select current_user, (select rolsuper or rolcreaterole or rolcreatedb "
            "from pg_roles where rolname = 'mlflow')"
        ).fetchone()
    assert row == ("mlflow", False)
    for database in ("retail_app", "temporal", "temporal_visibility"):
        with pytest.raises(psycopg.OperationalError, match="permission denied"):
            psycopg.connect(stack.dsn("mlflow", MLFLOW_PASSWORD, database))
    with pytest.raises(psycopg.OperationalError, match="permission denied"):
        psycopg.connect(stack.dsn("retail_app", APP_PASSWORD, "mlflow"))


def test_grafana_datasource_and_dashboard_are_provisioned(stack: Stack) -> None:
    auth = ("admin", GRAFANA_PASSWORD)
    source = httpx.get(
        stack.url("grafana", "/api/datasources/uid/prometheus"), auth=auth
    )
    source.raise_for_status()
    assert source.json()["type"] == "prometheus"
    health = httpx.get(
        stack.url("grafana", "/api/datasources/uid/prometheus/health"), auth=auth
    )
    assert health.json()["status"] == "OK"
    dashboards = httpx.get(
        stack.url("grafana", "/api/search"), params={"type": "dash-db"}, auth=auth
    ).json()
    assert "ra-local-telemetry" in {item["uid"] for item in dashboards}
    # Anonymous access is off.
    assert httpx.get(stack.url("grafana", "/api/datasources")).status_code == 401


def test_synthetic_metric_and_sample_trace_survive_restart(stack: Stack) -> None:
    _push_synthetic_metric(stack)
    trace_id = _create_sample_trace(stack)
    _eventually(lambda: _metric_values(stack) == ["42"], "metric in Prometheus")
    assert _trace_state(stack, trace_id) == "OK"

    stack.compose("restart", "prometheus", "mlflow", "grafana")
    stack.up()

    _eventually(lambda: _metric_values(stack) == ["42"], "metric after restart")
    _eventually(
        lambda: _metric_values(stack, base="grafana") == ["42"],
        "metric through Grafana after restart",
    )
    _eventually(lambda: _trace_state(stack, trace_id) == "OK", "trace after restart")
