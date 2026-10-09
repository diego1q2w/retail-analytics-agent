"""``dev_up`` against an isolated Compose project, free ports and a temp env file.

The repository ``.env`` and the default ``retail-analytics-local`` project are
never touched: the command runs with ``--env-file`` in a temporary directory and
``--project ra-test-*``. Fixture mode's scripted model answers every question
with a fixed notice, so a run completes without credentials.

The default (local execution) runs one API process over PostgreSQL only: no
Temporal container, namespace or worker. ``--execution-backend temporal``
brings up the durable stack as before. Switching never takes over the other
backend's active runs: startup refuses with instructions.
"""

from __future__ import annotations

import json
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
import sqlalchemy as sa

from retail_analytics.bootstrap import local_env
from retail_analytics.bootstrap.config import ENV_FILE_VARIABLE
from retail_analytics.bootstrap.local_setup import is_config_name
from tests.integration.compose_stack import (
    COMPOSE_FILE,
    ROOT,
    _free_port,
    _require_docker,
    unique_project,
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

    def tool_env(self, **extra: str) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not is_config_name(k)}
        env[ENV_FILE_VARIABLE] = str(self.env_file)
        env.update(extra)
        return env

    def tool(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", *args],
            cwd=ROOT,
            env=self.tool_env(),
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        )

    def cli(self, token: str, *args: str) -> subprocess.CompletedProcess[str]:
        """The ``analytics`` client, configured only through its own settings."""
        return subprocess.run(
            [sys.executable, "-m", "retail_analytics.bootstrap.cli", *args],
            cwd=ROOT,
            env=self.tool_env(
                CLI_API_URL=self.base_url,
                CLI_TOKEN=token,
                CLI_TIMEOUT_SECONDS="30",
            ),
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )

    def services(self) -> set[str]:
        listed = subprocess.run(
            [  # noqa: S607
                "docker",
                "compose",
                "-f",
                str(COMPOSE_FILE),
                "-p",
                self.project,
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
        )
        return set(listed.stdout.split())

    def sql(self, statement: str, **params: str) -> list[tuple[object, ...]]:
        url = self.values[local_env.DATABASE_URL_KEY]
        engine = sa.create_engine(url)
        try:
            with engine.begin() as connection:
                result = connection.execute(sa.text(statement), params)
                return [tuple(row) for row in result] if result.returns_rows else []
        finally:
            engine.dispose()


@pytest.fixture(scope="module")
def setup(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Setup]:
    _require_docker()
    base = tmp_path_factory.mktemp("dev-up")
    project = unique_project()
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
                f"\nAPP_API_PORT={api_port}\n"
                f"ARTIFACT_DIR={base / 'artifacts'}\n"
                # Telemetry is on by default: keep this stack off the default
                # host ports so a developer's own stack is never involved.
                f"COMPOSE_MLFLOW_PORT={ports['mlflow']}\n"
                f"COMPOSE_PROMETHEUS_PORT={ports['prometheus']}\n"
                f"COMPOSE_GRAFANA_PORT={ports['grafana']}\n"
                f"TELEMETRY_TRACES_ENDPOINT="
                f"http://127.0.0.1:{ports['mlflow']}/v1/traces\n"
                f"TELEMETRY_METRICS_ENDPOINT="
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
        key = uuid.uuid4().hex  # a new session and run on every call
        session = http.post("/v1/sessions", json={"submission_key": key + "-s"})
        assert session.status_code == 201, session.text
        url = f"/v1/sessions/{session.json()['session_id']}/runs"
        body = {"text": "How were sales last month?", "submission_key": key}
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


def test_default_runs_one_api_with_local_execution_then_ctrl_c_stops_it(
    setup: Setup,
) -> None:
    dev = setup.start()
    try:
        dev.wait_for("[dev] ready")
        assert f"API {setup.base_url}" in dev.text
        assert "execution: local" in dev.text
        assert "no Temporal, no worker" in dev.text
        assert "[worker]" not in dev.text
        assert "[api] " in dev.text
        health = httpx.get(setup.base_url + "/healthz").json()
        assert health["status"] == "ok" and health["execution_backend"] == "local"
        # PostgreSQL (and telemetry) only: no Temporal server or namespace job.
        running = setup.services()
        assert "postgres" in running
        assert not running & {"temporal", "temporal-schema", "temporal-namespace"}
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
        # The CLI is the client: status names the backend, ask runs a question.
        status = setup.cli(token, "status")
        assert status.returncode == 0, status.stderr
        assert "execution=local" in status.stdout
        asked = setup.cli(token, "ask", "How were sales last month?", "--json")
        assert asked.returncode in {0, 3}, asked.stdout + asked.stderr
        run_id = json.loads(asked.stdout)["run"]["run_id"]
        owner = setup.sql(
            "SELECT execution_backend, temporal_workflow_id FROM runs "
            "WHERE run_id = :run",
            run=run_id,
        )
        assert owner == [("local", None)]

        children = dev.children()
        assert set(children) == {"api"}
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


def test_worker_command_exits_promptly_with_local_execution(setup: Setup) -> None:
    started = time.monotonic()
    done = subprocess.run(
        [sys.executable, "-m", "retail_analytics.bootstrap.worker"],
        cwd=ROOT,
        env=setup.tool_env(),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert done.returncode == 3
    assert "EXECUTION_BACKEND=temporal" in done.stderr
    assert time.monotonic() - started < 30


def test_a_crashed_api_makes_dev_exit_non_zero(setup: Setup) -> None:
    dev = setup.start()
    try:
        dev.wait_for("[dev] ready")
        children = dev.children()
        assert set(children) == {"api"}
        os.kill(children["api"], signal.SIGKILL)
        assert dev.process.wait(timeout=60) != 0
        wait_gone(list(children.values()))
        assert "[dev] api exited unexpectedly" in dev.text
        assert not port_open(setup.api_port)
    finally:
        if dev.process.poll() is None:
            dev.process.kill()
            dev.process.wait()


@pytest.mark.parametrize("victim", ["worker", "api"])
def test_temporal_a_crashed_child_stops_the_other_and_exits_non_zero(
    setup: Setup, victim: str
) -> None:
    dev = setup.start("--execution-backend", "temporal")
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


def test_temporal_opt_in_runs_temporal_worker_and_api(setup: Setup) -> None:
    dev = setup.start("--execution-backend", "temporal")
    try:
        dev.wait_for("[dev] ready", timeout=600)
        assert "execution: temporal" in dev.text
        assert "[worker] investigation worker ready" in dev.text
        assert httpx.get(setup.base_url + "/healthz").json()["execution_backend"] == (
            "temporal"
        )
        assert "temporal" in setup.services()
        setup.tool("retail_analytics.bootstrap.dev_access", "provision")
        token = setup.tool(
            "retail_analytics.bootstrap.dev_access", "token", "demo-a"
        ).stdout.strip()
        view = run_one_question(setup, token)
        assert view["status"] in {"completed", "partial"}, view
        owner = setup.sql(
            "SELECT execution_backend FROM runs WHERE run_id = :run",
            run=str(view["run_id"]),
        )
        assert owner == [("temporal",)]
        children = dev.children()
        assert set(children) == {"worker", "api"}
        dev.process.send_signal(signal.SIGINT)
        assert dev.process.wait(timeout=60) == 0, dev.text[-2000:]
        wait_gone(list(children.values()))
    finally:
        if dev.process.poll() is None:
            dev.process.kill()
            dev.process.wait()
    # The selection was for that run only: the env file is unchanged.
    values = local_env.parse_values(setup.env_file.read_text(encoding="utf-8"))
    assert values[local_env.EXECUTION_BACKEND_KEY] == "local"


def test_switching_never_takes_over_active_temporal_runs(setup: Setup) -> None:
    """A Temporal run left active blocks local startup with instructions; going
    back to Temporal finishes it, and local execution then starts."""
    token = setup.tool(
        "retail_analytics.bootstrap.dev_access", "token", "demo-a"
    ).stdout.strip()
    # The Temporal API alone: the run is recorded and its workflow started, but
    # no worker executes it, so it stays active.
    api = subprocess.Popen(
        [sys.executable, "-m", "retail_analytics.bootstrap.api"],
        cwd=ROOT,
        env=setup.tool_env(EXECUTION_BACKEND="temporal"),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        deadline = time.monotonic() + 60
        while not port_open(setup.api_port):
            assert time.monotonic() < deadline and api.poll() is None
            time.sleep(0.3)
        headers = {"Authorization": f"Bearer {token}"}
        with httpx.Client(base_url=setup.base_url, headers=headers, timeout=30) as h:
            session = h.post("/v1/sessions", json={"submission_key": "own-s"})
            started = h.post(
                f"/v1/sessions/{session.json()['session_id']}/runs",
                json={"text": "How were sales last month?", "submission_key": "own"},
            )
            assert started.status_code == 202, started.text
            run_id = started.json()["run_id"]
    finally:
        api.terminate()
        api.wait(timeout=30)
    active = "SELECT status, execution_backend FROM runs WHERE run_id = :run"
    assert setup.sql(active, run=run_id) == [("running", "temporal")]

    refused = setup.start()
    assert refused.process.wait(timeout=240) == 1
    time.sleep(0.5)
    assert "were started with Temporal execution" in refused.text
    assert run_id in refused.text
    assert "--execution-backend temporal" in refused.text
    assert "[dev] api exited unexpectedly" in refused.text
    # Nothing was converted, interrupted or reassigned.
    assert setup.sql(active, run=run_id) == [("running", "temporal")]

    back = setup.start("--execution-backend", "temporal")
    try:
        back.wait_for("[dev] ready", timeout=600)
        deadline = time.monotonic() + 120
        unfinished = {"running", "waiting_for_input", "cancelling"}
        while setup.sql(active, run=run_id)[0][0] in unfinished:
            assert time.monotonic() < deadline, (
                f"the Temporal run did not finish: {setup.sql(active, run=run_id)}"
                f"\n{back.text[-4000:]}"
            )
            time.sleep(0.5)
        # Fixture mode answers completed or partial; Temporal finished it.
        assert setup.sql(active, run=run_id)[0] in {
            ("completed", "temporal"),
            ("partial", "temporal"),
        }
        back.process.send_signal(signal.SIGINT)
        assert back.process.wait(timeout=60) == 0, back.text[-2000:]
    finally:
        if back.process.poll() is None:
            back.process.kill()
            back.process.wait()

    local = setup.start()
    try:
        local.wait_for("[dev] ready")
        assert "execution: local" in local.text
        local.process.send_signal(signal.SIGINT)
        assert local.process.wait(timeout=60) == 0, local.text[-2000:]
    finally:
        if local.process.poll() is None:
            local.process.kill()
            local.process.wait()


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
    assert setup.values["TELEMETRY_ENABLED"] == "true"
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
