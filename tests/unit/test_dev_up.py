"""``dev_up``: supervision, readiness, shutdown and preflight (no Docker)."""

from __future__ import annotations

import io
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

from retail_analytics.bootstrap import dev_up, local_env, local_setup
from retail_analytics.bootstrap.dev_up import ChildSpec, DevUpError, Supervisor

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
        "RETAIL_ANALYTICS_DATABASE_URL=postgresql+psycopg://u:p@127.0.0.1:1/db\n"
        "RETAIL_ANALYTICS_TEMPORAL_ADDRESS=127.0.0.1:1\n"
        f"RETAIL_ANALYTICS_AUTH_SIGNING_KEY={'k' * 40}\n" + extra,
        encoding="utf-8",
    )
    return path


def test_settings_come_only_from_the_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RETAIL_ANALYTICS_API_PORT", "9999")  # stray shell variable
    ctx = local_setup.SetupContext(
        root=local_setup.ROOT,
        env_file=_env_file(tmp_path, "RETAIL_ANALYTICS_API_PORT=18080\n"),
        project="ra-unit",
    )
    ctx.refresh_values()
    settings = dev_up.load_settings(ctx.child_env())
    assert settings.api_port == 18080
    assert dev_up.api_base_url(settings) == "http://127.0.0.1:18080"


def test_missing_required_settings_name_the_variables(tmp_path: Path) -> None:
    path = tmp_path / "bare.env"
    path.write_text("RETAIL_ANALYTICS_MODE=fixture\n", encoding="utf-8")
    ctx = local_setup.SetupContext(
        root=local_setup.ROOT, env_file=path, project="ra-unit"
    )
    ctx.refresh_values()
    with pytest.raises(DevUpError, match=r"DATABASE_URL.*AUTH_SIGNING_KEY"):
        dev_up.load_settings(ctx.child_env())


def test_unreachable_backing_services_fail_fast(tmp_path: Path) -> None:
    ctx = local_setup.SetupContext(
        root=local_setup.ROOT, env_file=_env_file(tmp_path), project="ra-unit"
    )
    ctx.refresh_values()
    settings = dev_up.load_settings(ctx.child_env())
    with pytest.raises(DevUpError, match=r"PostgreSQL is not reachable.*Temporal"):
        dev_up.check_backing_services(settings)


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
    modules = {spec.name: spec.argv[2:] for spec in dev_up.child_specs()}
    assert modules == {
        "worker": ("retail_analytics.bootstrap.worker",),
        "api": ("retail_analytics.bootstrap.api",),
    }
    assert all(spec.argv[1] == "-m" for spec in dev_up.child_specs())


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
    assert dev_up.token_hint(default) == "retail-analytics-dev-access token demo-a"


def test_next_steps_recommend_the_one_command(tmp_path: Path) -> None:
    ctx = local_setup.SetupContext(
        root=local_setup.ROOT, env_file=tmp_path / "x.env", project="p"
    )
    text = "\n".join(local_setup.next_steps(ctx))
    assert "./scripts/dev.sh --env-file " + str(tmp_path / "x.env") in text
    assert local_env.PREFIX + "ENV_FILE=" in text
