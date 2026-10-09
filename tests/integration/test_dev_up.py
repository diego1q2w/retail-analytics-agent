"""``dev_up`` against an isolated Compose project, free ports and a temp env file.

The repository ``.env`` and the default ``retail-analytics-local`` project are
never touched: the command runs with ``--env-file`` in a temporary directory and
``--project ra-test-*``. The worker runs in fixture mode, whose scripted model
answers every question with a fixed notice, so a run completes without
credentials.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from retail_analytics.bootstrap import local_env
from retail_analytics.bootstrap.config import ENV_FILE_VARIABLE
from tests.integration.compose_stack import (
    COMPOSE_FILE,
    ROOT,
    _free_port,
    _require_docker,
)

pytestmark = pytest.mark.docker


@dataclass
class Dev:
    process: subprocess.Popen[str]
    lines: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        assert self.process.stdout is not None
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.lines.append(line.rstrip("\n"))

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def wait_for(self, needle: str, timeout: float = 240) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if needle in self.text:
                return
            if self.process.poll() is not None:
                time.sleep(0.3)
                break
            time.sleep(0.2)
        raise AssertionError(f"{needle!r} not seen; output:\n{self.text[-3000:]}")

    def children(self) -> dict[str, int]:
        found = subprocess.run(
            ["pgrep", "-P", str(self.process.pid)],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
        names: dict[str, int] = {}
        for raw in found.stdout.split():
            command = subprocess.run(
                ["ps", "-o", "command=", "-p", raw],  # noqa: S607
                capture_output=True,
                text=True,
                check=False,
            ).stdout
            for name in ("worker", "api"):
                if f"retail_analytics.bootstrap.{name}" in command:
                    names[name] = int(raw)
        return names


@dataclass
class Setup:
    project: str
    env_file: Path
    api_port: int
    values: dict[str, str]
    telemetry_ports: dict[str, int] = field(default_factory=dict)

    def start(self, *extra: str) -> Dev:
        return Dev(
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "retail_analytics.bootstrap.dev_up",
                    "--env-file",
                    str(self.env_file),
                    "--project",
                    self.project,
                    *extra,
                ],
                cwd=ROOT,
                env=os.environ.copy(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        )

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.api_port}"

    def secrets(self) -> list[str]:
        return local_env.secret_values(self.values)

    def tool(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("RETAIL_")}
        env[ENV_FILE_VARIABLE] = str(self.env_file)
        return subprocess.run(
            [sys.executable, "-m", *args],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        )


@pytest.fixture(scope="module")
def setup(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Setup]:
    _require_docker()
    base = tmp_path_factory.mktemp("dev-up")
    project = "ra-test-" + uuid.uuid4().hex[:8]
    api_port = _free_port()
    ports = {
        "mlflow": _free_port(),
        "prometheus": _free_port(),
        "grafana": _free_port(),
    }
    env_file = base / "dev.env"
    try:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "retail_analytics.bootstrap.local_setup",
                "--env-file",
                str(env_file),
                "--project",
                project,
                "--postgres-port",
                str(_free_port()),
                "--temporal-port",
                str(_free_port()),
                "--env-only",
            ],
            cwd=ROOT,
            env={k: v for k, v in os.environ.items() if not k.startswith("RETAIL_")},
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        )
        with env_file.open("a", encoding="utf-8") as handle:
            handle.write(
                f"\nRETAIL_ANALYTICS_API_PORT={api_port}\n"
                f"RETAIL_ANALYTICS_ARTIFACT_DIR={base / 'artifacts'}\n"
                # Telemetry is on by default: keep this stack off the default
                # host ports so a developer's own stack is never involved.
                f"COMPOSE_MLFLOW_PORT={ports['mlflow']}\n"
                f"COMPOSE_PROMETHEUS_PORT={ports['prometheus']}\n"
                f"COMPOSE_GRAFANA_PORT={ports['grafana']}\n"
                f"RETAIL_ANALYTICS_TELEMETRY_TRACES_ENDPOINT="
                f"http://127.0.0.1:{ports['mlflow']}/v1/traces\n"
                f"RETAIL_ANALYTICS_TELEMETRY_METRICS_ENDPOINT="
                f"http://127.0.0.1:{ports['prometheus']}/api/v1/otlp/v1/metrics\n"
            )
        values = local_env.parse_values(env_file.read_text(encoding="utf-8"))
        yield Setup(project, env_file, api_port, values, ports)
    finally:
        subprocess.run(
            [  # noqa: S607
                "docker",
                "compose",
                "-f",
                str(COMPOSE_FILE),
                "-p",
                project,
                "down",
                "--volumes",
                "--remove-orphans",
            ],
            env=os.environ.copy(),
            capture_output=True,
            check=False,
            timeout=300,
        )


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_gone(pids: list[int], timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while any(alive(pid) for pid in pids):
        assert time.monotonic() < deadline, f"processes still alive: {pids}"
        time.sleep(0.2)


def port_open(port: int) -> bool:
    try:
        httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=1)
    except httpx.HTTPError:
        return False
    return True


def run_one_question(setup: Setup, token: str) -> dict[str, object]:
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(base_url=setup.base_url, headers=headers, timeout=30) as http:
        session = http.post("/v1/sessions", json={"submission_key": "s"})
        assert session.status_code == 201, session.text
        url = f"/v1/sessions/{session.json()['session_id']}/runs"
        body = {"text": "How were sales last month?", "submission_key": "q"}
        deadline = time.monotonic() + 90
        while True:  # Temporal may still be warming up: 503, resend the same key
            started = http.post(url, json=body)
            if started.status_code != 503 or time.monotonic() > deadline:
                break
            time.sleep(0.5)
        assert started.status_code == 202, started.text
        run_id = started.json()["run_id"]
        while time.monotonic() < deadline:
            view = http.get(f"/v1/runs/{run_id}").json()
            if view["status"] in {"completed", "partial", "failed"}:
                return dict(view)
            time.sleep(0.5)
    raise AssertionError("run did not finish")


def test_one_command_runs_api_and_worker_then_ctrl_c_stops_both(setup: Setup) -> None:
    dev = setup.start()
    try:
        dev.wait_for("[dev] ready")
        assert f"API {setup.base_url}" in dev.text
        assert "[worker] investigation worker ready" in dev.text
        assert "[api] " in dev.text
        assert httpx.get(setup.base_url + "/healthz").json()["status"] == "ok"
        # Telemetry stack starts by default and its URLs are printed.
        mlflow, grafana = (
            setup.telemetry_ports["mlflow"],
            setup.telemetry_ports["grafana"],
        )
        assert f"[dev] MLflow: http://127.0.0.1:{mlflow}" in dev.text
        assert f"[dev] Grafana: http://127.0.0.1:{grafana}" in dev.text
        assert httpx.get(f"http://127.0.0.1:{mlflow}/health", timeout=10).is_success
        assert httpx.get(
            f"http://127.0.0.1:{grafana}/api/health", timeout=10
        ).is_success

        setup.tool("retail_analytics.bootstrap.dev_access", "provision")
        token = setup.tool(
            "retail_analytics.bootstrap.dev_access", "token", "demo-a"
        ).stdout.strip()
        view = run_one_question(setup, token)
        assert view["status"] in {"completed", "partial"}, view

        children = dev.children()
        assert set(children) == {"worker", "api"}
        dev.process.send_signal(signal.SIGINT)
        assert dev.process.wait(timeout=60) == 0, dev.text[-2000:]
        wait_gone(list(children.values()))
        assert not port_open(setup.api_port)
    finally:
        if dev.process.poll() is None:
            dev.process.kill()
            dev.process.wait()
    time.sleep(0.5)
    text = dev.text
    assert "[dev] stopped" in text
    assert token not in text
    assert all(secret not in text for secret in setup.secrets())


@pytest.mark.parametrize("victim", ["worker", "api"])
def test_a_crashed_child_stops_the_other_and_exits_non_zero(
    setup: Setup, victim: str
) -> None:
    dev = setup.start()
    try:
        dev.wait_for("[dev] ready")
        children = dev.children()
        assert set(children) == {"worker", "api"}
        os.kill(children[victim], signal.SIGKILL)
        assert dev.process.wait(timeout=60) != 0
        wait_gone(list(children.values()))
        assert f"[dev] {victim} exited unexpectedly" in dev.text
        assert not port_open(setup.api_port)
    finally:
        if dev.process.poll() is None:
            dev.process.kill()
            dev.process.wait()


def test_a_busy_port_is_refused_before_anything_starts(setup: Setup) -> None:
    import socket

    with socket.socket() as taken:
        taken.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        taken.bind(("127.0.0.1", setup.api_port))
        taken.listen()
        dev = setup.start()
        assert dev.process.wait(timeout=60) == 2
        time.sleep(0.5)
        assert f"port {setup.api_port}" in dev.text
        assert "[worker]" not in dev.text


def test_no_telemetry_skips_the_stack_and_runs_survive_the_default_on_outage(
    setup: Setup,
) -> None:
    """Opt-out starts no telemetry service; with the services down and the
    exporters still on (the default), runs complete unaffected."""
    subprocess.run(
        [  # noqa: S607
            "docker",
            "compose",
            "-f",
            str(COMPOSE_FILE),
            "-p",
            setup.project,
            "stop",
            "mlflow",
            "prometheus",
            "grafana",
        ],
        env=os.environ.copy(),
        capture_output=True,
        check=True,
        timeout=300,
    )
    assert setup.values["RETAIL_ANALYTICS_TELEMETRY_ENABLED"] == "true"
    dev = setup.start("--no-telemetry")
    try:
        dev.wait_for("[dev] ready")
        assert "Grafana" not in dev.text and "MLflow" not in dev.text
        running = subprocess.run(
            [  # noqa: S607
                "docker",
                "compose",
                "-f",
                str(COMPOSE_FILE),
                "-p",
                setup.project,
                "ps",
                "--services",
                "--status",
                "running",
            ],
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        ).stdout.split()
        assert not set(running) & {"mlflow", "prometheus", "grafana"}
        token = setup.tool(
            "retail_analytics.bootstrap.dev_access", "token", "demo-a"
        ).stdout.strip()
        started = time.monotonic()
        view = run_one_question(setup, token)
        assert view["status"] in {"completed", "partial"}, view
        assert time.monotonic() - started < 60
        dev.process.send_signal(signal.SIGINT)
        assert dev.process.wait(timeout=60) == 0, dev.text[-2000:]
    finally:
        if dev.process.poll() is None:
            dev.process.kill()
            dev.process.wait()
