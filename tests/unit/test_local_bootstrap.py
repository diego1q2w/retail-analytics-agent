"""Local bootstrap: environment reconciliation and the ordered step list."""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from retail_analytics.bootstrap import config, local_env, local_setup
from retail_analytics.bootstrap.config import RuntimeMode
from retail_analytics.bootstrap.local_setup import (
    BootstrapStep,
    SetupContext,
    StepFailed,
    StepResult,
)

TEMPLATE = (local_setup.ROOT / ".env.example").read_text(encoding="utf-8")


def _generated_values(text: str) -> list[str]:
    values = local_env.parse_values(text)
    keys = [*local_env.GENERATED_SECRETS, *local_env.COMPOSE_VOLUME_PASSWORDS]
    return [values[k] for k in keys]


def test_fresh_file_has_every_template_key_and_generated_secrets() -> None:
    result = local_env.reconcile(TEMPLATE, None)
    values = local_env.parse_values(result.text)
    assert set(local_env.parse_values(TEMPLATE)) <= set(values)
    for key in local_env.GENERATED_SECRETS:
        assert len(values[key].encode()) >= 32
    assert values[local_env.DATABASE_URL_KEY].startswith("postgresql+psycopg://")
    assert values[local_env.TEMPORAL_ADDRESS_KEY] == "127.0.0.1:57233"
    # The connection string uses the generated password for the new volume.
    assert values["COMPOSE_APP_DB_PASSWORD"] in values[local_env.DATABASE_URL_KEY]
    rendered = {r.key: r.render() for r in result.reports}
    assert rendered["AUTH_SIGNING_KEY"] == "<generated>"
    assert rendered["GEMINI_API_KEY"].startswith("<missing: ")
    assert "docs/google-access.md" in rendered["GEMINI_API_KEY"]
    assert rendered["OPENAI_API_KEY"] == "<empty: optional>"
    assert rendered["APP_MODE"] == "<kept>"


def test_reports_never_contain_secret_values() -> None:
    result = local_env.reconcile(TEMPLATE, None)
    shown = "\n".join(r.render() + r.detail for r in result.reports)
    shown += "\n".join(result.warnings)
    for value in _generated_values(result.text):
        assert value not in shown


def test_rerun_is_byte_identical() -> None:
    first = local_env.reconcile(TEMPLATE, None)
    second = local_env.reconcile(TEMPLATE, first.text)
    assert not second.changed
    assert second.text == first.text


def test_existing_values_are_never_changed_or_reordered() -> None:
    existing = (
        "# my notes\n"
        "REFERENCE_KEY=my-own-reference-key-that-is-long-enough-xx\n"
        "ZZZ_CUSTOM=keep me\n"
        "GEMINI_API_KEY='abc def'\n"
        "AUTH_SIGNING_KEY=\n"
    )
    result = local_env.reconcile(TEMPLATE, existing)
    assert result.changed
    old_lines = existing.splitlines()
    new_lines = result.text.splitlines()
    for old in old_lines:
        if old != "AUTH_SIGNING_KEY=":
            assert new_lines.index(old) is not None
    positions = [new_lines.index(line) for line in old_lines[:4]]
    assert positions == sorted(positions)
    values = local_env.parse_values(result.text)
    assert values["REFERENCE_KEY"].startswith("my-own-reference")
    assert values["GEMINI_API_KEY"] == "abc def"
    assert len(values["AUTH_SIGNING_KEY"]) >= 32
    reference = next(r for r in result.reports if r.key == "REFERENCE_KEY")
    assert reference.status is local_env.Status.KEPT
    assert "invalidates" in reference.detail


def test_existing_volume_keeps_compose_defaults() -> None:
    result = local_env.reconcile(TEMPLATE, None, new_postgres_volume=False)
    values = local_env.parse_values(result.text)
    assert values["COMPOSE_APP_DB_PASSWORD"] == ""
    assert "local-only-app" in values[local_env.DATABASE_URL_KEY]
    assert any("existing postgres volume" in w for w in result.warnings)


def test_keys_are_read_from_the_template_not_hardcoded() -> None:
    template = TEMPLATE + "FUTURE_SETTING=on\nFUTURE_EMPTY=\n"
    first = local_env.reconcile(template, None)
    values = local_env.parse_values(first.text)
    assert values["FUTURE_SETTING"] == "on"
    assert "FUTURE_EMPTY" in values
    # An older environment file gains the new keys without losing anything.
    older = local_env.reconcile(TEMPLATE, None).text
    upgraded = local_env.reconcile(template, older)
    assert upgraded.changed
    assert upgraded.text.startswith(older.rstrip("\n"))
    assert local_env.parse_values(upgraded.text)["FUTURE_SETTING"] == "on"


def test_port_overrides_apply_to_a_new_file_and_the_connection_defaults() -> None:
    result = local_env.reconcile(
        TEMPLATE,
        None,
        overrides={"COMPOSE_POSTGRES_PORT": "61001", "COMPOSE_TEMPORAL_PORT": "61002"},
    )
    values = local_env.parse_values(result.text)
    assert values["COMPOSE_POSTGRES_PORT"] == "61001"
    assert "@127.0.0.1:61001/" in values[local_env.DATABASE_URL_KEY]
    assert values[local_env.TEMPORAL_ADDRESS_KEY] == "127.0.0.1:61002"


def test_interactive_prompt_fills_only_empty_external_credentials() -> None:
    asked: list[tuple[str, bool]] = []

    def prompt(key: str, secret: bool) -> str:
        asked.append((key, secret))
        return "typed-value" if key.endswith("GEMINI_API_KEY") else ""

    result = local_env.reconcile(TEMPLATE, None, prompt=prompt)
    values = local_env.parse_values(result.text)
    assert values["GEMINI_API_KEY"] == "typed-value"
    assert ("GEMINI_API_KEY", True) in asked
    assert ("BIGQUERY_PROJECT", False) in asked
    assert "AUTH_SIGNING_KEY" not in [k for k, _ in asked]


def test_redaction_scrubs_secret_values() -> None:
    values = {"GEMINI_API_KEY": "sekret-value-123", "OTHER": "visible-value"}
    hidden = local_env.secret_values(values)
    text = local_env.redact("boom sekret-value-123 visible-value", hidden)
    assert "sekret" not in text and "visible-value" in text


def _ctx(tmp_path: Path, **kwargs: object) -> SetupContext:
    lines: list[str] = []
    ctx = SetupContext(
        root=local_setup.ROOT,
        env_file=tmp_path / "x.env",
        project="ra-unit",
        echo=lines.append,
        **kwargs,  # type: ignore[arg-type]
    )
    ctx.env_file.write_text("GEMINI_API_KEY=very-secret-value-xyz\n")
    ctx.refresh_values()
    return ctx


def test_child_output_is_scrubbed_and_failures_are_reported(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    out = ctx.python("-c", "import os;print(os.environ['GEMINI_API_KEY'])")
    assert "very-secret" not in out and "<redacted>" in out
    with pytest.raises(StepFailed) as raised:
        ctx.python("-c", "import sys;print('very-secret-value-xyz');sys.exit(3)")
    assert "very-secret" not in str(raised.value) and "status 3" in str(raised.value)


def test_steps_run_in_order_and_stop_at_a_required_failure(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    ran: list[str] = []

    def ok(name: str) -> BootstrapStep:
        def run(_: SetupContext) -> StepResult:
            ran.append(name)
            return StepResult()

        return BootstrapStep(name, name, run)

    def fail(_: SetupContext) -> StepResult:
        raise StepFailed("nope")

    def warn(_: SetupContext) -> StepResult:
        raise StepFailed("soft")

    steps = [
        ok("a"),
        BootstrapStep("skipped", "x", fail, enabled=lambda _: False),
        BootstrapStep("soft", "x", warn, required=False),
        ok("b"),
        BootstrapStep("hard", "x", fail),
        ok("never"),
    ]
    with pytest.raises(StepFailed):
        local_setup.run_steps(ctx, steps)
    assert ran == ["a", "b"]


def test_registered_steps_are_unique_and_ordered_sensibly() -> None:
    names = [s.name for s in local_setup.STEPS]
    assert len(names) == len(set(names))
    order = ["environment", "docker", "services", "migrate", "executives"]
    assert [n for n in names if n in order] == order
    assert names.index("golden-seeds") > names.index("executives")
    assert names.index("golden-embeddings") > names.index("golden-seeds")
    assert names.index("migrate") > names.index("services")


def test_env_only_cli_creates_a_private_file_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(local_setup, "_postgres_volume_exists", lambda _: False)
    env_file = tmp_path / "sub" / "local.env"
    runner = CliRunner()
    args = ["--env-file", str(env_file), "--env-only", "--project", "ra-unit"]
    first = runner.invoke(local_setup.main, args)
    assert first.exit_code == 0, first.output
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    before = env_file.read_bytes()
    for value in _generated_values(before.decode()):
        assert value not in first.output
    assert "<generated>" in first.output and "<missing:" in first.output
    second = runner.invoke(local_setup.main, args)
    assert second.exit_code == 0, second.output
    assert env_file.read_bytes() == before
    assert "no changes" in second.output
    assert "<generated>" not in second.output


def test_list_steps() -> None:
    result = CliRunner().invoke(local_setup.main, ["--list-steps"])
    assert result.exit_code == 0
    assert "golden-seeds" in result.output
    assert sys.executable  # keep import of sys meaningful for future steps


# --- isolation: --env-file never reads the repository .env ------------------

_PRINT_LOCATION = (
    "from retail_analytics.bootstrap.config import load_backend_settings;"
    "print(load_backend_settings().bigquery_location)"
)


def _isolation_ctx(tmp_path: Path, env_text: str) -> SetupContext:
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".env").write_text(
        "BIGQUERY_LOCATION=SENTINEL-FROM-REPO-ENV\n", encoding="utf-8"
    )
    env_file = tmp_path / "isolated.env"
    env_file.write_text("APP_MODE=fixture\n" + env_text, encoding="utf-8")
    ctx = SetupContext(
        root=root, env_file=env_file, project="p", echo=lambda _line: None
    )
    ctx.refresh_values()
    return ctx


def test_child_commands_never_read_the_repository_env(tmp_path: Path) -> None:
    ctx = _isolation_ctx(tmp_path, "BIGQUERY_LOCATION=\n")
    out = ctx.python("-c", _PRINT_LOCATION)
    assert "SENTINEL" not in out
    assert out == "US"  # the default, not the repository value


def test_child_commands_use_the_env_file_values(tmp_path: Path) -> None:
    ctx = _isolation_ctx(tmp_path, "BIGQUERY_LOCATION=EU\n")
    assert ctx.python("-c", _PRINT_LOCATION) == "EU"


def test_child_env_drops_stray_parent_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BIGQUERY_LOCATION", "SENTINEL-FROM-SHELL")
    monkeypatch.setenv("CLI_API_URL", "http://stray.invalid")
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "kept")
    monkeypatch.setenv("RETAIL_ANALYTICS_BIGQUERY_LOCATION", "LEGACY-FROM-SHELL")
    monkeypatch.setenv("ANALYTICS_CLI_TOKEN", "legacy-token-from-shell")
    monkeypatch.setenv("DATABASE_URL", "postgresql://other-project/db")
    monkeypatch.setenv("UNRELATED_TOOL_SETTING", "kept")
    ctx = _isolation_ctx(tmp_path, "BIGQUERY_LOCATION=\n")
    env = ctx.child_env()
    assert "BIGQUERY_LOCATION" not in env
    assert "CLI_API_URL" not in env
    assert "RETAIL_ANALYTICS_BIGQUERY_LOCATION" not in env
    assert "ANALYTICS_CLI_TOKEN" not in env
    assert "DATABASE_URL" not in env
    assert env["UNRELATED_TOOL_SETTING"] == "kept"
    assert env["COMPOSE_PROJECT_NAME"] == "kept"
    assert env[config.ENV_FILE_VARIABLE] == str(ctx.env_file)
    assert ctx.python("-c", _PRINT_LOCATION) == "US"


def test_loader_pointer_replaces_the_default_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text("BIGQUERY_LOCATION=SENTINEL\n")
    pointed = tmp_path / "other.env"
    pointed.write_text("APP_API_PORT=9191\n")
    monkeypatch.setenv(config.ENV_FILE_VARIABLE, str(pointed))
    settings = config.load_backend_settings()
    assert settings.api_port == 9191
    assert settings.bigquery_location == "US"
    # A process variable still overrides the pointed file.
    monkeypatch.setenv("APP_API_PORT", "9292")
    assert config.load_backend_settings().api_port == 9292


def test_loader_pointer_to_a_missing_file_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(config.ENV_FILE_VARIABLE, str(tmp_path / "missing.env"))
    with pytest.raises(config.ConfigError, match="ENV_FILE"):
        config.load_backend_settings()


def test_default_loading_without_the_pointer_is_unchanged(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("APP_API_PORT=9393\n")
    assert config.load_backend_settings().api_port == 9393


def test_declared_source_currency_defaults_to_usd_without_overwriting() -> None:
    key = "SOURCE_CURRENCY_DECLARED"
    fresh = local_env.parse_values(local_env.reconcile(TEMPLATE, None).text)
    assert fresh[key] == "USD"
    kept = local_env.reconcile(TEMPLATE, f"{key}=EUR\n")
    assert local_env.parse_values(kept.text)[key] == "EUR"


# --- telemetry on by default (T30-F1) ---------------------------------------

TELEMETRY_KEY = "TELEMETRY_ENABLED"


def _env_only(env_file: Path, *extra: str) -> str:
    result = CliRunner().invoke(
        local_setup.main,
        ["--env-file", str(env_file), "--env-only", "--project", "ra-unit", *extra],
    )
    assert result.exit_code == 0, result.output
    return result.output


def test_telemetry_defaults_to_on_in_settings_and_template() -> None:
    assert config.BackendSettings(mode=RuntimeMode.FIXTURE).telemetry_enabled is True
    assert config.load_backend_settings(
        environ={"APP_MODE": "fixture"}, env_file=None
    ).telemetry_enabled
    assert local_env.parse_values(TEMPLATE)[TELEMETRY_KEY] == "true"


def test_env_only_adds_the_key_as_true_to_an_existing_file(tmp_path: Path) -> None:
    env_file = tmp_path / "old.env"
    env_file.write_text("APP_MODE=fixture\n", encoding="utf-8")
    _env_only(env_file)
    assert local_env.parse_values(env_file.read_text())[TELEMETRY_KEY] == "true"


def test_env_only_never_overwrites_an_explicit_false(tmp_path: Path) -> None:
    env_file = tmp_path / "old.env"
    env_file.write_text(f"{TELEMETRY_KEY}=false\n", encoding="utf-8")
    _env_only(env_file)
    _env_only(env_file, "--telemetry")
    assert local_env.parse_values(env_file.read_text())[TELEMETRY_KEY] == "false"


def test_no_telemetry_writes_false_only_for_a_new_key(tmp_path: Path) -> None:
    fresh = tmp_path / "fresh.env"
    _env_only(fresh, "--no-telemetry")
    assert local_env.parse_values(fresh.read_text())[TELEMETRY_KEY] == "false"
    kept = tmp_path / "kept.env"
    kept.write_text(f"{TELEMETRY_KEY}=true\n", encoding="utf-8")
    _env_only(kept, "--no-telemetry")
    assert local_env.parse_values(kept.read_text())[TELEMETRY_KEY] == "true"


def test_telemetry_flag_is_an_accepted_no_op(tmp_path: Path) -> None:
    plain, flagged = tmp_path / "a.env", tmp_path / "b.env"
    _env_only(plain)
    _env_only(flagged, "--telemetry")
    assert local_env.parse_values(plain.read_text())[TELEMETRY_KEY] == "true"
    assert local_env.parse_values(flagged.read_text())[TELEMETRY_KEY] == "true"


def _tele_ctx(tmp_path: Path, env_text: str, telemetry: bool = True) -> SetupContext:
    env_file = tmp_path / "t.env"
    env_file.write_text("APP_MODE=fixture\n" + env_text, encoding="utf-8")
    ctx = SetupContext(
        root=local_setup.ROOT,
        env_file=env_file,
        project="ra-unit",
        echo=lambda _line: None,
        telemetry=telemetry,
    )
    ctx.refresh_values()
    return ctx


def test_the_telemetry_step_runs_by_default_and_skips_on_opt_out(
    tmp_path: Path,
) -> None:
    step = next(s for s in local_setup.STEPS if s.name == "telemetry")
    assert step.enabled(_tele_ctx(tmp_path, f"{TELEMETRY_KEY}=true\n"))
    assert step.enabled(_tele_ctx(tmp_path, ""))
    assert not step.enabled(_tele_ctx(tmp_path, "", telemetry=False))
    assert not step.enabled(_tele_ctx(tmp_path, f"{TELEMETRY_KEY}=false\n"))
    names = [s.name for s in local_setup.STEPS]
    assert names.index("telemetry") == names.index("services") + 1


def test_next_steps_print_the_grafana_and_mlflow_urls(tmp_path: Path) -> None:
    text = "\n".join(local_setup.next_steps(_tele_ctx(tmp_path, "")))
    assert "Grafana http://127.0.0.1:53000" in text
    assert "MLflow http://127.0.0.1:55500" in text
    custom = _tele_ctx(
        tmp_path, "COMPOSE_GRAFANA_PORT=53111\nCOMPOSE_MLFLOW_PORT=55111\n"
    )
    shown = "\n".join(local_setup.next_steps(custom))
    assert "127.0.0.1:53111" in shown and "127.0.0.1:55111" in shown
    off = "\n".join(local_setup.next_steps(_tele_ctx(tmp_path, "", telemetry=False)))
    assert "Grafana" not in off


# --- execution backend (local default, Temporal opt-in) ---------------------

BACKEND_KEY = local_env.EXECUTION_BACKEND_KEY
# An environment file written by bootstrap before the selector existed.
OLD_ENV = (
    f"APP_MODE=fixture\n"
    f"APP_DATABASE_URL=postgresql+psycopg://retail_app:x@127.0.0.1:55442/retail_app\n"
    f"{local_env.TEMPORAL_ADDRESS_KEY}=127.0.0.1:57233\n"
)


def test_template_and_settings_default_to_local_execution() -> None:
    assert local_env.parse_values(TEMPLATE)[BACKEND_KEY] == "local"
    assert (
        config.BackendSettings(mode=RuntimeMode.FIXTURE).execution_backend.value
        == "local"
    )


def test_existing_file_without_selector_adopts_local_and_explains() -> None:
    result = local_env.reconcile(TEMPLATE, OLD_ENV)
    values = local_env.parse_values(result.text)
    assert values[BACKEND_KEY] == "local"
    # Old Temporal values stay as they were: nothing is removed.
    assert values[local_env.TEMPORAL_ADDRESS_KEY] == "127.0.0.1:57233"
    assert result.text.startswith(OLD_ENV)
    assert local_env.LOCAL_ADOPTED_NOTE in result.warnings
    again = local_env.reconcile(TEMPLATE, result.text)
    assert not again.changed and again.text == result.text
    assert local_env.LOCAL_ADOPTED_NOTE not in again.warnings


def test_explicit_temporal_is_preserved() -> None:
    existing = OLD_ENV + f"{BACKEND_KEY}=temporal\n"
    result = local_env.reconcile(TEMPLATE, existing, overrides={BACKEND_KEY: "local"})
    assert local_env.parse_values(result.text)[BACKEND_KEY] == "temporal"
    assert local_env.LOCAL_ADOPTED_NOTE not in result.warnings


def test_option_selects_temporal_only_for_a_new_key(tmp_path: Path) -> None:
    old = tmp_path / "old.env"
    old.write_text(OLD_ENV, encoding="utf-8")
    output = _env_only(old, "--execution-backend", "temporal")
    assert local_env.parse_values(old.read_text())[BACKEND_KEY] == "temporal"
    assert "was added as local" not in output
    snapshot = old.read_bytes()
    output = _env_only(old, "--execution-backend", "local")
    # An explicit choice is never rewritten; the run says so.
    assert old.read_bytes() == snapshot
    assert "this run uses local execution" in output


def test_rerun_on_an_old_file_explains_the_transition(tmp_path: Path) -> None:
    old = tmp_path / "old.env"
    old.write_text(OLD_ENV, encoding="utf-8")
    first = _env_only(old)
    assert "was added as local, the new default" in first
    snapshot = old.read_bytes()
    second = _env_only(old)
    assert old.read_bytes() == snapshot
    assert "was added as local" not in second


def _backend_ctx(
    tmp_path: Path, env_text: str, option: str | None = None
) -> tuple[SetupContext, list[tuple[str, ...]]]:
    env_file = tmp_path / "b.env"
    env_file.write_text("APP_MODE=fixture\n" + env_text, encoding="utf-8")
    calls: list[tuple[str, ...]] = []

    class Recording(SetupContext):
        def compose(self, *args: str, timeout: int = 0) -> str:
            calls.append(args)
            return ""

    ctx = Recording(
        root=local_setup.ROOT,
        env_file=env_file,
        project="ra-unit",
        echo=lambda _line: None,
        execution_backend=option,
    )
    ctx.refresh_values()
    return ctx, calls


def test_services_step_starts_only_postgres_with_local_execution(
    tmp_path: Path,
) -> None:
    ctx, calls = _backend_ctx(tmp_path, OLD_ENV)  # no selector: local
    result = local_setup.step_services(ctx)
    assert calls == [("up", "-d", "--wait", "postgres")]
    assert "no Temporal" in result.message
    assert "temporal" not in " ".join(" ".join(c) for c in calls)


def test_services_step_starts_temporal_when_selected(tmp_path: Path) -> None:
    for text, option in (
        (OLD_ENV + f"{BACKEND_KEY}=temporal\n", None),
        (OLD_ENV, "temporal"),
    ):
        ctx, calls = _backend_ctx(tmp_path, text, option)
        local_setup.step_services(ctx)
        assert calls == [
            ("up", "-d", "--wait", "postgres", "temporal"),
            ("run", "--rm", "temporal-namespace"),
        ]


def test_option_overrides_the_file_for_child_commands(tmp_path: Path) -> None:
    ctx, _ = _backend_ctx(tmp_path, f"{BACKEND_KEY}=temporal\n", "local")
    assert ctx.child_env()[BACKEND_KEY] == "local"
    assert local_setup.selected_backend(ctx) == "local"
    plain, _ = _backend_ctx(tmp_path, f"{BACKEND_KEY}=temporal\n")
    assert plain.child_env()[BACKEND_KEY] == "temporal"


def test_invalid_selector_in_the_file_fails_the_services_step(
    tmp_path: Path,
) -> None:
    ctx, calls = _backend_ctx(tmp_path, f"{BACKEND_KEY}=kubernetes\n")
    with pytest.raises(StepFailed, match=BACKEND_KEY):
        local_setup.step_services(ctx)
    assert calls == []


def test_next_steps_name_the_execution_backend(tmp_path: Path) -> None:
    local, _ = _backend_ctx(tmp_path, OLD_ENV)
    text = "\n".join(local_setup.next_steps(local))
    assert "Execution: local (default)" in text
    temporal, _ = _backend_ctx(tmp_path, OLD_ENV, "temporal")
    shown = "\n".join(local_setup.next_steps(temporal))
    assert "Execution: Temporal (opt-in)" in shown
    assert "--execution-backend temporal" in shown


# --- migration of older prefixed names (A05) ----------------------------------

LEGACY_SECRET = "legacy-secret-value-0123456789-abcdef"
LEGACY_FILE = (
    "# my local settings\n"
    "RETAIL_ANALYTICS_MODE=fixture\n"
    "\n"
    "# the key\n"
    f"export RETAIL_ANALYTICS_GEMINI_API_KEY='{LEGACY_SECRET}'\n"
    "RETAIL_ANALYTICS_API_PORT=8181  # inline comment\n"
    "COMPOSE_POSTGRES_PORT=55442\n"
    "ANALYTICS_CLI_TOKEN_FILE=/tmp/token\n"
    "RETAIL_ANALYTICS_EXECUTION_BACKEND=temporal\n"
)


def test_migration_renames_keys_preserving_order_comments_and_values() -> None:
    result = local_env.migrate_legacy(LEGACY_FILE)
    assert result.changed
    assert result.text == (
        "# my local settings\n"
        "APP_MODE=fixture\n"
        "\n"
        "# the key\n"
        f"export GEMINI_API_KEY='{LEGACY_SECRET}'\n"
        "APP_API_PORT=8181  # inline comment\n"
        "COMPOSE_POSTGRES_PORT=55442\n"
        "CLI_TOKEN_FILE=/tmp/token\n"
        "EXECUTION_BACKEND=temporal\n"
    )
    assert ("ANALYTICS_CLI_TOKEN_FILE", "CLI_TOKEN_FILE") in result.renamed
    assert result.conflicts == ()
    again = local_env.migrate_legacy(result.text)
    assert not again.changed
    assert again.text == result.text


def test_migration_conflict_keeps_the_current_key() -> None:
    text = (
        "GEMINI_API_KEY=current-value\n"
        f"RETAIL_ANALYTICS_GEMINI_API_KEY={LEGACY_SECRET}\n"
    )
    result = local_env.migrate_legacy(text)
    assert result.conflicts == (("RETAIL_ANALYTICS_GEMINI_API_KEY", "GEMINI_API_KEY"),)
    values = local_env.parse_values(result.text)
    assert values == {"GEMINI_API_KEY": "current-value"}
    assert local_env.migrate_legacy(result.text).changed is False


def test_env_only_migrates_an_existing_file_in_place(tmp_path: Path) -> None:
    env_file = tmp_path / "legacy.env"
    env_file.write_text(
        LEGACY_FILE
        + "GEMINI_API_KEY=x\n"
        + f"RETAIL_ANALYTICS_GEMINI_API_KEY={LEGACY_SECRET}2\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    output = _env_only(env_file)
    assert LEGACY_SECRET not in output
    assert "RETAIL_ANALYTICS_MODE -> APP_MODE" in output
    assert "conflict: RETAIL_ANALYTICS_GEMINI_API_KEY and GEMINI_API_KEY" in output
    text = env_file.read_text(encoding="utf-8")
    values = local_env.parse_values(text)
    assert not [k for k in values if config.is_legacy_name(k)]
    assert values["APP_API_PORT"] == "8181"
    assert values["EXECUTION_BACKEND"] == "temporal"
    assert values["GEMINI_API_KEY"] == "x"  # the current key wins
    assert text.startswith("# my local settings\nAPP_MODE=fixture\n")
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    # The migrated file loads, and a rerun changes nothing.
    config.load_backend_settings(environ={}, env_file=env_file)
    assert "no changes" in _env_only(env_file)
    assert env_file.read_text(encoding="utf-8") == text


def test_env_only_warns_about_legacy_names_exported_by_the_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RETAIL_ANALYTICS_DATABASE_URL", LEGACY_SECRET)
    output = _env_only(tmp_path / "new.env")
    assert "RETAIL_ANALYTICS_DATABASE_URL" in output
    assert LEGACY_SECRET not in output


def test_live_defaults_require_external_credentials(tmp_path: Path) -> None:
    assert local_env.parse_values(TEMPLATE)["APP_MODE"] == "live"
    ctx = _isolation_ctx(tmp_path, "")
    ctx.values.pop("APP_MODE", None)
    with pytest.raises(StepFailed, match="BIGQUERY_PROJECT and GEMINI_API_KEY"):
        local_setup.step_check_credentials(ctx)
    assert next(s for s in local_setup.STEPS if s.name == "check-credentials").required


def test_fixture_skips_external_credential_checks(tmp_path: Path) -> None:
    ctx = _isolation_ctx(tmp_path, "BIGQUERY_PROJECT=fake\nGEMINI_API_KEY=fake\n")
    result = local_setup.step_check_credentials(ctx)
    assert result.status == "skipped"
    assert "fixed responses" in result.message
