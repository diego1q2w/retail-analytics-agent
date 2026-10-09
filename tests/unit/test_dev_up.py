"""``dev_up``: supervision, readiness, shutdown and preflight (no Docker)."""

from __future__ import annotations

import io
import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

from retail_analytics.bootstrap import dev_up, local_setup
from retail_analytics.bootstrap.dev_up import ChildSpec, DevUpError, Supervisor
from retail_analytics.domain.runs import ExecutionBackend

SECRET = "s3cret-value-1234567890"


def py(code: str) -> tuple[str, ...]:
    return (sys.executable, "-u", "-c", code)


def make(specs: list[ChildSpec], out: io.StringIO, *, grace: float = 2.0) -> Supervisor:
    return Supervisor(
        specs,
        env={"PATH": "/usr/bin:/bin"},
        cwd=Path.cwd(),
        hidden=[SECRET],
        ready_markers={"a": "READY"},
        out=out,
        grace_seconds=grace,
    )


def test_output_is_prefixed_and_scrubbed() -> None:
    out = io.StringIO()
    sup = make(
        [
            ChildSpec("a", py(f"print('hello READY {SECRET}')")),
            ChildSpec("b", py("print('from b')")),
        ],
        out,
    )
    sup.start()
    assert sup.wait_until(lambda: sup.worker_ready("a"), 10) is None
    sup.shutdown()
    text = out.getvalue()
    assert "[a] hello READY <redacted>" in text
    assert "[b] from b" in text
    assert SECRET not in text


def test_shutdown_terminates_children_then_kills_stubborn_ones() -> None:
    out = io.StringIO()
    stubborn = (
        "import signal,time;signal.signal(signal.SIGTERM, signal.SIG_IGN);"
        "print('READY');time.sleep(60)"
    )
    sup = make(
        [
            ChildSpec("a", py(stubborn)),
            ChildSpec("b", py("import time;print('up');time.sleep(60)")),
        ],
        out,
        grace=0.5,
    )
    sup.start()
    assert sup.wait_until(lambda: sup.worker_ready("a"), 10) is None
    time.sleep(0.3)
    started = time.monotonic()
    sup.shutdown()
    assert time.monotonic() - started < 10
    assert sup.exited() is not None
    assert all(c.process.poll() is not None for c in sup._children)
    assert "a ignored SIGTERM; killing it" in out.getvalue()


def test_child_exit_is_detected_and_named() -> None:
    out = io.StringIO()
    sup = make(
        [
            ChildSpec("a", py("import time;print('READY');time.sleep(60)")),
            ChildSpec("b", py("import sys;sys.exit(3)")),
        ],
        out,
    )
    sup.start()
    gone = None
    deadline = time.monotonic() + 10
    while gone is None and time.monotonic() < deadline:
        gone = sup.run_until_event() if sup.exited() else None
        time.sleep(0.05)
    assert gone == ("b", 3)
    sup.shutdown()
    assert all(c.process.poll() is not None for c in sup._children)


def test_request_stop_releases_the_wait() -> None:
    out = io.StringIO()
    sup = make([ChildSpec("a", py("import time;time.sleep(60)"))], out)
    sup.start()
    threading.Timer(0.3, sup.request_stop).start()
    assert sup.run_until_event() is None
    sup.shutdown()


def test_port_in_use_is_refused_with_an_actionable_message() -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        with pytest.raises(DevUpError, match=f"port {port}.*API_PORT"):
            dev_up.check_port_free("127.0.0.1", port)
    dev_up.check_port_free("127.0.0.1", port)  # free again


def _env_file(tmp_path: Path, extra: str = "") -> Path:
    path = tmp_path / "dev.env"
    path.write_text(
        "APP_MODE=fixture\n"
        "APP_DATABASE_URL=postgresql+psycopg://u:p@127.0.0.1:1/db\n"
        "TEMPORAL_ADDRESS=127.0.0.1:1\n"
        f"AUTH_SIGNING_KEY={'k' * 40}\n" + extra,
        encoding="utf-8",
    )
    return path


def test_settings_come_only_from_the_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_API_PORT", "9999")  # stray shell variable
    ctx = local_setup.SetupContext(
        root=local_setup.ROOT,
        env_file=_env_file(tmp_path, "APP_API_PORT=18080\n"),
        project="ra-unit",
    )
    ctx.refresh_values()
    settings = dev_up.load_settings(ctx.child_env())
    assert settings.api_port == 18080
    assert dev_up.api_base_url(settings) == "http://127.0.0.1:18080"


def test_missing_required_settings_name_the_variables(tmp_path: Path) -> None:
    path = tmp_path / "bare.env"
    path.write_text("APP_MODE=fixture\n", encoding="utf-8")
    ctx = local_setup.SetupContext(
        root=local_setup.ROOT, env_file=path, project="ra-unit"
    )
    ctx.refresh_values()
    with pytest.raises(DevUpError, match=r"APP_DATABASE_URL.*AUTH_SIGNING_KEY"):
        dev_up.load_settings(ctx.child_env())


def test_unreachable_backing_services_fail_fast(tmp_path: Path) -> None:
    ctx = local_setup.SetupContext(
        root=local_setup.ROOT, env_file=_env_file(tmp_path), project="ra-unit"
    )
    ctx.refresh_values()
    settings = dev_up.load_settings(ctx.child_env())
    # Local execution: an old Temporal address is not checked.
    with pytest.raises(DevUpError) as local:
        dev_up.check_backing_services(settings)
    assert "PostgreSQL is not reachable" in str(local.value)
    assert "Temporal" not in str(local.value)
    ctx.execution_backend = "temporal"
    settings = dev_up.load_settings(ctx.child_env())
    with pytest.raises(DevUpError, match=r"PostgreSQL is not reachable.*Temporal"):
        dev_up.check_backing_services(settings)


def test_temporal_execution_requires_the_temporal_address(tmp_path: Path) -> None:
    path = tmp_path / "t.env"
    path.write_text(
        "APP_MODE=fixture\n"
        "EXECUTION_BACKEND=temporal\n"
        "APP_DATABASE_URL=postgresql+psycopg://u:p@127.0.0.1:1/db\n"
        f"AUTH_SIGNING_KEY={'k' * 40}\n",
        encoding="utf-8",
    )
    ctx = local_setup.SetupContext(
        root=local_setup.ROOT, env_file=path, project="ra-unit"
    )
    ctx.refresh_values()
    with pytest.raises(DevUpError, match="TEMPORAL_ADDRESS"):
        dev_up.load_settings(ctx.child_env())
    # The same file with local execution for this run needs no Temporal.
    ctx.execution_backend = "local"
    assert dev_up.load_settings(ctx.child_env()).temporal_address is None


def test_adoption_hint_explains_the_local_default_once(tmp_path: Path) -> None:
    ctx = local_setup.SetupContext(
        root=local_setup.ROOT, env_file=_env_file(tmp_path), project="ra-unit"
    )
    ctx.refresh_values()
    hint = dev_up.adoption_hint(ctx)
    assert hint is not None and "local execution" in hint and "temporal" in hint
    explicit = _env_file(tmp_path, "EXECUTION_BACKEND=local\n")
    ctx = local_setup.SetupContext(
        root=local_setup.ROOT, env_file=explicit, project="ra-unit"
    )
    ctx.refresh_values()
    assert dev_up.adoption_hint(ctx) is None


def test_missing_env_file_points_at_bootstrap(tmp_path: Path) -> None:
    ctx = local_setup.SetupContext(
        root=local_setup.ROOT, env_file=tmp_path / "nope.env", project="ra-unit"
    )
    with pytest.raises(DevUpError, match=r"bootstrap\.sh"):
        dev_up.run_dev(ctx, services=False, install_signals=False)


def test_ready_marker_matches_the_worker() -> None:
    source = (local_setup.ROOT / "src/retail_analytics/bootstrap/worker.py").read_text()
    assert dev_up.WORKER_READY_MARKER in source


def test_child_specs_run_the_real_entry_points() -> None:
    local = {spec.name: spec.argv[2:] for spec in dev_up.child_specs()}
    assert local == {"api": ("retail_analytics.bootstrap.api",)}
    temporal = dev_up.child_specs(ExecutionBackend.TEMPORAL)
    assert {spec.name: spec.argv[2:] for spec in temporal} == {
        "worker": ("retail_analytics.bootstrap.worker",),
        "api": ("retail_analytics.bootstrap.api",),
    }
    assert all(spec.argv[1] == "-m" for spec in temporal)


def test_token_hint_points_at_a_custom_env_file_without_a_token(
    tmp_path: Path,
) -> None:
    custom = local_setup.SetupContext(
        root=local_setup.ROOT, env_file=tmp_path / "x.env", project="p"
    )
    assert str(tmp_path / "x.env") in dev_up.token_hint(custom)
    default = local_setup.SetupContext(
        root=local_setup.ROOT, env_file=local_setup.ROOT / ".env", project="p"
    )
    assert dev_up.token_hint(default) == "retail-analytics-dev-access token local-admin"


def test_next_steps_recommend_the_one_command(tmp_path: Path) -> None:
    ctx = local_setup.SetupContext(
        root=local_setup.ROOT, env_file=tmp_path / "x.env", project="p"
    )
    text = "\n".join(local_setup.next_steps(ctx))
    assert "./scripts/dev.sh --env-file " + str(tmp_path / "x.env") in text
    assert "APP_ENV_FILE=" in text


def test_dev_runs_the_telemetry_step_with_the_services_by_default() -> None:
    assert dev_up.SERVICE_STEPS == ("docker", "services", "telemetry", "migrate")
    from click.testing import CliRunner

    help_text = CliRunner().invoke(dev_up.main, ["--help"]).output
    assert "--telemetry" in help_text and "--no-telemetry" in help_text


def test_dev_starts_the_services_and_prints_the_urls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default run: the telemetry step executes and the URLs print; no Docker."""
    ran: list[str] = []
    monkeypatch.setattr(dev_up, "check_port_free", lambda *_: None)
    monkeypatch.setattr(dev_up, "healthz_ok", lambda *_: True)
    env = tmp_path / "d.env"
    env.write_text(
        "APP_MODE=fixture\n"
        "APP_DATABASE_URL=postgresql://u:p@127.0.0.1:1/db\n"
        "TEMPORAL_ADDRESS=127.0.0.1:1\n"
        f"AUTH_SIGNING_KEY={'k' * 40}\n",
        encoding="utf-8",
    )

    def recorder(name: str) -> local_setup.BootstrapStep:
        original = next(s for s in local_setup.STEPS if s.name == name)

        def run(_ctx: local_setup.SetupContext) -> local_setup.StepResult:
            ran.append(name)
            return local_setup.StepResult()

        return local_setup.BootstrapStep(
            name, original.summary, run, enabled=original.enabled
        )

    monkeypatch.setattr(
        local_setup, "STEPS", tuple(recorder(s.name) for s in local_setup.STEPS)
    )
    child = py(
        "import time; print('investigation worker ready', flush=True); time.sleep(60)"
    )

    def run_once(telemetry: bool) -> list[str]:
        ran.clear()
        lines: list[str] = []
        ctx = local_setup.SetupContext(
            root=local_setup.ROOT,
            env_file=env,
            project="ra-unit",
            telemetry=telemetry,
            echo=lines.append,
        )
        stopper = threading.Timer(2.5, lambda: os.kill(os.getpid(), signal.SIGTERM))
        stopper.start()
        try:
            code = dev_up.run_dev(
                ctx,
                services=True,
                ready_timeout=20,
                specs=[ChildSpec("worker", child)],
                supervisor_out=io.StringIO(),
                stop_grace=2,
            )
        finally:
            stopper.cancel()
        assert code == 0
        return lines

    on = run_once(True)
    assert ran == ["docker", "services", "telemetry", "migrate"]
    assert any("Grafana: http://127.0.0.1:53000" in line for line in on)
    assert any("MLflow: http://127.0.0.1:55500" in line for line in on)
    assert any("execution: local" in line for line in on)
    assert any("no Temporal, no worker" in line for line in on)
    # The file predates the selector and still names Temporal: explained.
    assert any("is not set: using local execution" in line for line in on)
    off = run_once(False)
    assert ran == ["docker", "services", "migrate"]
    assert not any("Grafana" in line for line in off)


def test_temporal_readiness_waits_for_the_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With Temporal execution the API alone is not ready: the worker must
    report its connection; local execution needs no worker at all."""
    monkeypatch.setattr(dev_up, "check_port_free", lambda *_: None)
    monkeypatch.setattr(dev_up, "check_backing_services", lambda _settings: None)
    seen: list[ExecutionBackend | None] = []

    def healthz(_url: str, backend: ExecutionBackend | None = None) -> bool:
        seen.append(backend)
        return True

    monkeypatch.setattr(dev_up, "healthz_ok", healthz)
    lines: list[str] = []
    ctx = local_setup.SetupContext(
        root=local_setup.ROOT,
        env_file=_env_file(tmp_path),
        project="ra-unit",
        execution_backend="temporal",
        echo=lines.append,
    )
    silent = py("import time; time.sleep(60)")
    code = dev_up.run_dev(
        ctx,
        services=False,
        ready_timeout=1.5,
        specs=[ChildSpec("worker", silent), ChildSpec("api", silent)],
        install_signals=False,
        supervisor_out=io.StringIO(),
        stop_grace=2,
    )
    assert code == 1
    assert any("still waiting for worker (Temporal connection)" in x for x in lines)
    assert set(seen) == {ExecutionBackend.TEMPORAL}
