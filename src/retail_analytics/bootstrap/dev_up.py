"""One local command that runs the backend: ``./scripts/dev.sh``.

Local development only; a small foreground supervisor for a laptop, not a
process manager to deploy. What it runs follows the execution backend
(``EXECUTION_BACKEND``, or ``--execution-backend`` for this
run):

- ``local`` (default): one ``retail-analytics-api`` process that hosts the
  investigations; PostgreSQL only, no Temporal, no worker.
- ``temporal`` (opt-in): Temporal plus ``retail-analytics-worker`` and
  ``retail-analytics-api`` (in production these are separate services).

What it does, in order:

1. loads the environment file with the same isolation as the bootstrap (only
   that file, never a stray ``.env`` or parent-shell setting variables);
2. checks the API port is free (refuses with an actionable message otherwise);
3. unless ``--no-services``, reuses the bootstrap steps to make sure Docker,
   PostgreSQL (and Temporal with its namespace, for the Temporal backend) and
   the migrations are in place (compose project from
   ``--project``; unrelated containers are never touched). MLflow, Prometheus
   and Grafana start too unless ``--no-telemetry`` (or the environment file
   sets ``TELEMETRY_ENABLED=false``);
4. starts ``retail_analytics.bootstrap.api`` (and, for Temporal,
   ``...bootstrap.worker``) as child processes, prefixes their output with
   ``[api]`` / ``[worker]`` and waits until ``/healthz`` answers for the
   selected backend (and the worker reports it is connected);
5. on Ctrl-C or SIGTERM stops them (SIGTERM, then SIGKILL after a grace
   period) and exits 0. If a child exits on its own, stops the others and
   exits 1 naming the one that failed.

Output never contains secret values: every line from a child is scrubbed of the
environment file's secret-looking values, and tokens are never printed (the
command to issue one is).
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

import click
from sqlalchemy.engine import make_url

from retail_analytics.bootstrap import local_env, local_setup
from retail_analytics.bootstrap.config import (
    ENV_FILE_VARIABLE,
    BackendSettings,
    ConfigError,
    load_backend_settings,
    missing_api_settings,
)
from retail_analytics.domain.runs import ExecutionBackend

# The line retail_analytics.bootstrap.worker prints once it is connected to
# Temporal and polling its task queue (a unit test keeps this in sync). Only
# the Temporal backend runs a worker.
WORKER_READY_MARKER = "investigation worker ready"
SERVICE_STEPS = ("docker", "services", "telemetry", "migrate")
READY_TIMEOUT_SECONDS = 90.0
STOP_GRACE_SECONDS = 10.0
POLL_SECONDS = 0.2

Echo = Callable[[str], None]


class DevUpError(Exception):
    """Start-up cannot proceed. The message is actionable and has no secrets."""


@dataclass(frozen=True)
class ChildSpec:
    name: str
    argv: tuple[str, ...]


@dataclass
class _Child:
    spec: ChildSpec
    process: subprocess.Popen[str]
    reader: threading.Thread
    ready: threading.Event = field(default_factory=threading.Event)


class Supervisor:
    """Runs named child processes with prefixed, scrubbed output.

    Children start in their own session so a terminal Ctrl-C reaches only this
    supervisor, which then stops them in order. ``request_stop`` is safe to call
    from a signal handler or another thread.
    """

    def __init__(
        self,
        specs: Sequence[ChildSpec],
        *,
        env: Mapping[str, str],
        cwd: Path,
        hidden: Sequence[str] = (),
        ready_markers: Mapping[str, str] | None = None,
        out: IO[str] | None = None,
        grace_seconds: float = STOP_GRACE_SECONDS,
    ) -> None:
        self._specs = tuple(specs)
        self._env = dict(env)
        self._cwd = cwd
        self._hidden = list(hidden)
        self._markers = dict(ready_markers or {})
        self._out = out or sys.stdout
        self._grace = grace_seconds
        self._write_lock = threading.Lock()
        self._stop = threading.Event()
        self._children: list[_Child] = []

    # --- output -----------------------------------------------------------

    def log(self, name: str, line: str) -> None:
        text = local_env.redact(line.rstrip("\n"), self._hidden)
        with self._write_lock:
            self._out.write(f"[{name}] {text}\n")
            self._out.flush()

    def _pump(self, child_name: str, stream: IO[str], ready: threading.Event) -> None:
        marker = self._markers.get(child_name)
        for line in stream:
            self.log(child_name, line)
            if marker and marker in line:
                ready.set()

    # --- lifecycle --------------------------------------------------------

    def request_stop(self) -> None:
        self._stop.set()

    @property
    def stop_requested(self) -> bool:
        return self._stop.is_set()

    def start(self) -> None:
        for spec in self._specs:
            process = subprocess.Popen(  # noqa: S603
                list(spec.argv),
                cwd=self._cwd,
                env=self._env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
            stream = process.stdout
            if stream is None:  # pragma: no cover - PIPE always provides it
                raise DevUpError(f"cannot read the output of {spec.name}")
            ready = threading.Event()
            reader = threading.Thread(
                target=self._pump,
                args=(spec.name, stream, ready),
                daemon=True,
            )
            reader.start()
            self._children.append(_Child(spec, process, reader, ready))

    def exited(self) -> tuple[str, int] | None:
        """The first child that has exited, as ``(name, status)``."""
        for child in self._children:
            status = child.process.poll()
            if status is not None:
                return child.spec.name, status
        return None

    def worker_ready(self, name: str) -> bool:
        return any(c.spec.name == name and c.ready.is_set() for c in self._children)

    def wait_until(
        self,
        condition: Callable[[], bool],
        timeout: float,
    ) -> str | None:
        """Wait for ``condition``. Return ``None`` when it holds, else why not:
        ``"stop"``, ``"timeout"`` or ``"exit"`` (a child died)."""
        deadline = time.monotonic() + timeout
        while True:
            if condition():
                return None
            if self._stop.is_set():
                return "stop"
            if self.exited() is not None:
                return "exit"
            if time.monotonic() >= deadline:
                return "timeout"
            time.sleep(POLL_SECONDS)

    def run_until_event(self) -> tuple[str, int] | None:
        """Block until stop is requested (``None``) or a child exits."""
        while not self._stop.is_set():
            gone = self.exited()
            if gone is not None:
                return gone
            time.sleep(POLL_SECONDS)
        return None

    def shutdown(self) -> None:
        """Stop every child: SIGTERM to its process group, SIGKILL after grace."""
        live = [c for c in self._children if c.process.poll() is None]
        for child in live:
            self._signal(child, signal.SIGTERM)
        deadline = time.monotonic() + self._grace
        for child in live:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                child.process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                self.log("dev", f"{child.spec.name} ignored SIGTERM; killing it")
                self._signal(child, signal.SIGKILL)
                child.process.wait()
        for child in self._children:
            # Reap any stragglers left in the group (for example grandchildren).
            self._signal(child, signal.SIGKILL)
            child.reader.join(timeout=5)
            if child.process.stdout is not None:
                child.process.stdout.close()

    @staticmethod
    def _signal(child: _Child, sig: int) -> None:
        # start_new_session makes the child's pid its process-group id.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(child.process.pid, sig)


# --- preflight -----------------------------------------------------------


def api_base_url(settings: BackendSettings) -> str:
    host = settings.api_host
    if host in {"0.0.0.0", "::", ""}:  # noqa: S104
        host = "127.0.0.1"
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{settings.api_port}"


def check_port_free(host: str, port: int) -> None:
    """Raise ``DevUpError`` when something already listens on ``host:port``."""
    target = "127.0.0.1" if host in {"0.0.0.0", "::", ""} else host  # noqa: S104
    try:
        with socket.create_connection((target, port), timeout=1):
            pass
    except OSError:
        return
    raise DevUpError(
        f"port {port} on {target} is already in use by another process (maybe "
        "an API from an earlier run). Stop it, or choose another port with "
        "APP_API_PORT in the environment file."
    )


def _reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def check_backing_services(settings: BackendSettings) -> None:
    """With ``--no-services``: fail early with a clear message if they are down."""
    problems: list[str] = []
    if settings.database_url is not None:
        url = make_url(settings.database_url.get_secret_value())
        if url.host and url.port and not _reachable(url.host, url.port):
            problems.append(f"PostgreSQL is not reachable at {url.host}:{url.port}")
    if (
        settings.execution_backend is ExecutionBackend.TEMPORAL
        and settings.temporal_address
    ):
        host, _, port = settings.temporal_address.rpartition(":")
        if host and port.isdigit() and not _reachable(host, int(port)):
            problems.append(f"Temporal is not reachable at {settings.temporal_address}")
    if problems:
        raise DevUpError(
            "; ".join(problems)
            + ". Start them (run without --no-services, or ./scripts/bootstrap.sh)."
        )


def load_settings(env: Mapping[str, str]) -> BackendSettings:
    try:
        settings = load_backend_settings(environ=env)
    except ConfigError as exc:
        raise DevUpError(str(exc)) from None
    missing = missing_api_settings(settings)
    if missing:
        raise DevUpError(
            "missing in the environment file: "
            + ", ".join(missing)
            + ". Run ./scripts/bootstrap.sh to complete it."
        )
    return settings


def healthz_ok(base_url: str, backend: ExecutionBackend | None = None) -> bool:
    """``/healthz`` answers (and, given ``backend``, reports that backend)."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(base_url + "/healthz", timeout=2) as response:
            if response.status != 200:
                return False
            if backend is None:
                return True
            body = json.loads(response.read() or b"{}")
            return bool(body.get("execution_backend") == backend.value)
    except (urllib.error.URLError, OSError, ValueError):
        return False


def child_specs(
    backend: ExecutionBackend = ExecutionBackend.LOCAL,
) -> tuple[ChildSpec, ...]:
    """The API alone (local execution), or the worker and the API (Temporal)."""
    python = sys.executable
    api = ChildSpec("api", (python, "-m", "retail_analytics.bootstrap.api"))
    if backend is ExecutionBackend.TEMPORAL:
        worker = ChildSpec(
            "worker", (python, "-m", "retail_analytics.bootstrap.worker")
        )
        return (worker, api)
    return (api,)


def adoption_hint(ctx: local_setup.SetupContext) -> str | None:
    """Explain the local default to an environment file from before it."""
    if ctx.execution_backend or ctx.values.get(local_env.EXECUTION_BACKEND_KEY):
        return None
    if not ctx.values.get(local_env.TEMPORAL_ADDRESS_KEY):
        return None
    return (
        f"{local_env.EXECUTION_BACKEND_KEY} is not set: using local execution "
        "(the default; Temporal is opt-in). Rerun ./scripts/bootstrap.sh to "
        "record it, or set it to temporal to keep Temporal execution."
    )


def token_hint(ctx: local_setup.SetupContext) -> str:
    prefix = ""
    if ctx.env_file.resolve() != (ctx.root / ".env").resolve():
        prefix = f"{ENV_FILE_VARIABLE}={ctx.env_file} "
    return f"{prefix}retail-analytics-dev-access token demo-a"


# --- orchestration ------------------------------------------------------


def run_dev(
    ctx: local_setup.SetupContext,
    *,
    services: bool = True,
    ready_timeout: float = READY_TIMEOUT_SECONDS,
    specs: Sequence[ChildSpec] | None = None,
    install_signals: bool = True,
    supervisor_out: IO[str] | None = None,
    stop_grace: float = STOP_GRACE_SECONDS,
) -> int:
    """Run the backend processes until stopped. Returns the exit code."""
    echo = ctx.echo
    if not ctx.env_file.is_file():
        raise DevUpError(
            f"environment file {ctx.env_file} does not exist. "
            "Run ./scripts/bootstrap.sh first."
        )
    ctx.refresh_values()
    env = ctx.child_env()
    settings = load_settings(env)
    check_port_free(settings.api_host, settings.api_port)
    if services:
        by_name = {step.name: step for step in local_setup.STEPS}
        try:
            local_setup.run_steps(ctx, [by_name[name] for name in SERVICE_STEPS])
        except local_setup.StepFailed:
            raise DevUpError(
                "could not bring up the local services (see above)"
            ) from None
    else:
        check_backing_services(settings)

    env["PYTHONUNBUFFERED"] = "1"
    backend = settings.execution_backend
    temporal = backend is ExecutionBackend.TEMPORAL
    supervisor = Supervisor(
        specs or child_specs(backend),
        env=env,
        cwd=ctx.root,
        hidden=local_env.secret_values(ctx.values),
        ready_markers={"worker": WORKER_READY_MARKER} if temporal else {},
        out=supervisor_out,
        # Local execution: let the API end its runs truthfully (interrupted)
        # within its own shutdown grace before it could be killed.
        grace_seconds=stop_grace
        if temporal
        else max(stop_grace, settings.local_shutdown_grace_seconds + 5),
    )
    base_url = api_base_url(settings)
    previous: dict[signal.Signals, Any] = {}
    if install_signals:
        for number in (signal.SIGINT, signal.SIGTERM):
            previous[number] = signal.signal(
                number, lambda _n, _f: supervisor.request_stop()
            )
    code = 0

    def ready() -> list[tuple[str, bool]]:
        checks = [
            (
                f"api (/healthz, {backend.value} execution)",
                healthz_ok(base_url, backend),
            )
        ]
        if temporal:
            checks.insert(
                0, ("worker (Temporal connection)", supervisor.worker_ready("worker"))
            )
        return checks

    try:
        hint = adoption_hint(ctx)
        if hint:
            echo(f"[dev] {hint}")
        echo(
            "[dev] starting worker and api (Temporal execution; local development only)"
            if temporal
            else "[dev] starting api (local execution: investigations run in the "
            "API process; no Temporal, no worker; local development only)"
        )
        supervisor.start()
        outcome = supervisor.wait_until(
            lambda: all(ok for _, ok in ready()), ready_timeout
        )
        if outcome == "timeout":
            waiting = [label for label, ok in ready() if not ok]
            echo(
                f"[dev] not ready after {ready_timeout:.0f}s; still waiting for "
                + ", ".join(waiting)
            )
            code = 1
        elif outcome is None:
            echo(
                f"[dev] ready: API {base_url}  (mode: {settings.mode.value}, "
                f"execution: {backend.value})"
            )
            if not temporal:
                echo(
                    "[dev] local execution: disconnecting the CLI keeps a run "
                    "going; stopping the API interrupts running investigations"
                )
            if local_setup.telemetry_wanted(ctx) and services:
                for name, url in local_setup.telemetry_urls(ctx).items():
                    echo(f"[dev] {name}: {url}")
            echo(f"[dev] dev token (printed to stdout only): {token_hint(ctx)}")
            echo("[dev] Ctrl-C stops " + ("both" if temporal else "it"))
            gone = supervisor.run_until_event()
            if gone is not None:
                code = _report_exit(echo, gone)
        elif outcome == "exit":
            echo("[dev] a process stopped before becoming ready")
            code = _report_exit(echo, supervisor.exited() or ("process", -1))
    finally:
        echo("[dev] stopping")
        supervisor.shutdown()
        for number, handler in previous.items():
            signal.signal(number, handler)
    echo("[dev] stopped")
    return code


def _report_exit(echo: Echo, gone: tuple[str, int]) -> int:
    name, status = gone
    echo(
        f"[dev] {name} exited unexpectedly with status {status}; "
        "stopping the other processes (if any)"
    )
    return 1


@click.command()
@click.option(
    "--env-file",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Environment file to use (default: .env in the repository).",
)
@click.option(
    "--project",
    default=None,
    help=(
        "Compose project name (default: COMPOSE_PROJECT_NAME or "
        f"{local_setup.DEFAULT_PROJECT})."
    ),
)
@click.option(
    "--execution-backend",
    type=click.Choice(local_env.EXECUTION_BACKENDS),
    default=None,
    help=(
        "Override EXECUTION_BACKEND for this run: local (the "
        "default; API only) or temporal (Temporal, worker and API)."
    ),
)
@click.option(
    "--no-services",
    is_flag=True,
    help=(
        "Do not touch Docker; only check PostgreSQL (and Temporal, with "
        "Temporal execution) are reachable."
    ),
)
@click.option(
    "--telemetry/--no-telemetry",
    default=True,
    help=(
        "Start mlflow, prometheus and grafana with the other services (the "
        "default; --telemetry is accepted for compatibility)."
    ),
)
@click.option(
    "--ready-timeout",
    type=click.FloatRange(min=1),
    default=READY_TIMEOUT_SECONDS,
    show_default=True,
    help="Seconds to wait for the worker and the API to become ready.",
)
def main(
    env_file: Path | None,
    project: str | None,
    execution_backend: str | None,
    no_services: bool,
    telemetry: bool,
    ready_timeout: float,
) -> None:
    """Run the backend for local development.

    Starts postgres and the telemetry stack (unless --no-services or
    --no-telemetry), applies migrations, then runs retail-analytics-api,
    which hosts the investigations (local execution, the default). With
    Temporal execution it also starts temporal and retail-analytics-worker.
    Ctrl-C stops everything it started. Not for production.
    """
    ctx = local_setup.SetupContext(
        root=local_setup.ROOT,
        env_file=(env_file or local_setup.ROOT / ".env").resolve(),
        project=project
        or os.environ.get("COMPOSE_PROJECT_NAME")
        or local_setup.DEFAULT_PROJECT,
        telemetry=telemetry,
        execution_backend=execution_backend,
    )
    try:
        code = run_dev(ctx, services=not no_services, ready_timeout=ready_timeout)
    except DevUpError as exc:
        click.echo(f"[dev] {exc}", err=True)
        raise SystemExit(2) from None
    raise SystemExit(code)


if __name__ == "__main__":
    main()
