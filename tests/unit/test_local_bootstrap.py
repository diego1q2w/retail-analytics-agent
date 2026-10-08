"""Local bootstrap: environment reconciliation and the ordered step list."""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from retail_analytics.bootstrap import config, local_env, local_setup
from retail_analytics.bootstrap.local_setup import (
    BootstrapStep,
    SetupContext,
    StepFailed,
    StepResult,
)

TEMPLATE = (local_setup.ROOT / ".env.example").read_text(encoding="utf-8")
P = local_env.PREFIX


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
    assert rendered[P + "AUTH_SIGNING_KEY"] == "<generated>"
    assert rendered[P + "GEMINI_API_KEY"].startswith("<missing: ")
    assert "docs/google-access.md" in rendered[P + "GEMINI_API_KEY"]
    assert rendered[P + "OPENAI_API_KEY"] == "<empty: optional>"
    assert rendered[P + "MODE"] == "<kept>"


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
        f"{P}REFERENCE_KEY=my-own-reference-key-that-is-long-enough-xx\n"
        "ZZZ_CUSTOM=keep me\n"
        f"{P}GEMINI_API_KEY='abc def'\n"
        f"{P}AUTH_SIGNING_KEY=\n"
    )
    result = local_env.reconcile(TEMPLATE, existing)
    assert result.changed
    old_lines = existing.splitlines()
    new_lines = result.text.splitlines()
    for old in old_lines:
        if old != f"{P}AUTH_SIGNING_KEY=":
            assert new_lines.index(old) is not None
    positions = [new_lines.index(line) for line in old_lines[:4]]
    assert positions == sorted(positions)
    values = local_env.parse_values(result.text)
    assert values[P + "REFERENCE_KEY"].startswith("my-own-reference")
    assert values[P + "GEMINI_API_KEY"] == "abc def"
    assert len(values[P + "AUTH_SIGNING_KEY"]) >= 32
    reference = next(r for r in result.reports if r.key == P + "REFERENCE_KEY")
    assert reference.status is local_env.Status.KEPT
    assert "invalidates" in reference.detail


def test_existing_volume_keeps_compose_defaults() -> None:
    result = local_env.reconcile(TEMPLATE, None, new_postgres_volume=False)
    values = local_env.parse_values(result.text)
    assert values["COMPOSE_APP_DB_PASSWORD"] == ""
    assert "local-only-app" in values[local_env.DATABASE_URL_KEY]
    assert any("existing postgres volume" in w for w in result.warnings)


def test_keys_are_read_from_the_template_not_hardcoded() -> None:
    template = TEMPLATE + f"{P}FUTURE_SETTING=on\n{P}FUTURE_EMPTY=\n"
    first = local_env.reconcile(template, None)
    values = local_env.parse_values(first.text)
    assert values[P + "FUTURE_SETTING"] == "on"
    assert P + "FUTURE_EMPTY" in values
    # An older environment file gains the new keys without losing anything.
    older = local_env.reconcile(TEMPLATE, None).text
    upgraded = local_env.reconcile(template, older)
    assert upgraded.changed
    assert upgraded.text.startswith(older.rstrip("\n"))
    assert local_env.parse_values(upgraded.text)[P + "FUTURE_SETTING"] == "on"


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
    assert values[P + "GEMINI_API_KEY"] == "typed-value"
    assert (P + "GEMINI_API_KEY", True) in asked
    assert (P + "BIGQUERY_PROJECT", False) in asked
    assert P + "AUTH_SIGNING_KEY" not in [k for k, _ in asked]


def test_redaction_scrubs_secret_values() -> None:
    values = {P + "GEMINI_API_KEY": "sekret-value-123", "OTHER": "visible-value"}
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
    ctx.env_file.write_text(f"{P}GEMINI_API_KEY=very-secret-value-xyz\n")
    ctx.refresh_values()
    return ctx


def test_child_output_is_scrubbed_and_failures_are_reported(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    out = ctx.python("-c", "import os;print(os.environ['" + P + "GEMINI_API_KEY'])")
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
        f"{P}BIGQUERY_LOCATION=SENTINEL-FROM-REPO-ENV\n", encoding="utf-8"
    )
    env_file = tmp_path / "isolated.env"
    env_file.write_text(env_text, encoding="utf-8")
    ctx = SetupContext(
        root=root, env_file=env_file, project="p", echo=lambda _line: None
    )
    ctx.refresh_values()
    return ctx


def test_child_commands_never_read_the_repository_env(tmp_path: Path) -> None:
    ctx = _isolation_ctx(tmp_path, f"{P}BIGQUERY_LOCATION=\n")
    out = ctx.python("-c", _PRINT_LOCATION)
    assert "SENTINEL" not in out
    assert out == "US"  # the default, not the repository value


def test_child_commands_use_the_env_file_values(tmp_path: Path) -> None:
    ctx = _isolation_ctx(tmp_path, f"{P}BIGQUERY_LOCATION=EU\n")
    assert ctx.python("-c", _PRINT_LOCATION) == "EU"


def test_child_env_drops_stray_parent_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(P + "BIGQUERY_LOCATION", "SENTINEL-FROM-SHELL")
    monkeypatch.setenv("ANALYTICS_CLI_API_URL", "http://stray.invalid")
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "kept")
    ctx = _isolation_ctx(tmp_path, f"{P}BIGQUERY_LOCATION=\n")
    env = ctx.child_env()
    assert P + "BIGQUERY_LOCATION" not in env
    assert "ANALYTICS_CLI_API_URL" not in env
    assert env["COMPOSE_PROJECT_NAME"] == "kept"
    assert env[config.ENV_FILE_VARIABLE] == str(ctx.env_file)
    assert ctx.python("-c", _PRINT_LOCATION) == "US"


def test_loader_pointer_replaces_the_default_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text(f"{P}BIGQUERY_LOCATION=SENTINEL\n")
    pointed = tmp_path / "other.env"
    pointed.write_text(f"{P}API_PORT=9191\n")
    monkeypatch.setenv(config.ENV_FILE_VARIABLE, str(pointed))
    settings = config.load_backend_settings()
    assert settings.api_port == 9191
    assert settings.bigquery_location == "US"
    # A process variable still overrides the pointed file.
    monkeypatch.setenv(P + "API_PORT", "9292")
    assert config.load_backend_settings().api_port == 9292


def test_loader_pointer_to_a_missing_file_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(config.ENV_FILE_VARIABLE, str(tmp_path / "missing.env"))
    with pytest.raises(config.ConfigError, match="ENV_FILE"):
        config.load_backend_settings()


def test_default_loading_without_the_pointer_is_unchanged(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(f"{P}API_PORT=9393\n")
    assert config.load_backend_settings().api_port == 9393


def test_declared_source_currency_defaults_to_usd_without_overwriting() -> None:
    key = P + "SOURCE_CURRENCY_DECLARED"
    fresh = local_env.parse_values(local_env.reconcile(TEMPLATE, None).text)
    assert fresh[key] == "USD"
    kept = local_env.reconcile(TEMPLATE, f"{key}=EUR\n")
    assert local_env.parse_values(kept.text)[key] == "EUR"
