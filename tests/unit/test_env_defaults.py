"""Bootstrap does not pin ordinary defaults and flags stale overrides."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from retail_analytics.bootstrap import config, local_env, local_setup
from retail_analytics.bootstrap.config import RuntimeMode

TEMPLATE = (local_setup.ROOT / ".env.example").read_text(encoding="utf-8")
STALE = "RUN_ACTIVE_SECONDS=600\nEMBEDDING_PROVIDER=hashing\n"


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("APP_MODE", "EMBEDDING_PROVIDER", "RUN_ACTIVE_SECONDS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(local_setup, "_postgres_volume_exists", lambda _: False)


def _run(env_file: Path) -> str:
    result = CliRunner().invoke(
        local_setup.main,
        ["--env-file", str(env_file), "--env-only", "--project", "ra-unit"],
    )
    assert result.exit_code == 0, result.output
    return result.output


def test_every_written_template_key_is_a_setup_value_not_a_default() -> None:
    fields = {**config.BACKEND_ENV_NAMES, **config.CLI_ENV_NAMES}
    by_env = {env: name for name, env in fields.items()}
    settings = config.BackendSettings(mode=RuntimeMode.FIXTURE)
    for key, value in local_env.parse_values(TEMPLATE).items():
        field = by_env.get(key)
        if field is None or value == "":
            continue  # COMPOSE_*, empty credentials and secrets
        default = getattr(settings, field, None)
        assert str(default).lower() != value.lower(), f"{key} pins its default"


def test_fresh_file_writes_only_setup_values_and_generated_secrets(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "new.env"
    _run(env_file)
    values = local_env.parse_values(env_file.read_text())
    for key in ("APP_MODE", "RUN_ACTIVE_SECONDS", "EMBEDDING_PROVIDER"):
        assert key not in values
    for key in local_env.GENERATED_SECRETS:
        assert len(values[key].encode()) >= 32
    assert values[local_env.DATABASE_URL_KEY].startswith("postgresql+psycopg://")
    # Documented, but only as a comment.
    assert "# RUN_ACTIVE_SECONDS=120" in env_file.read_text()


def test_repeat_runs_are_idempotent_and_do_not_re_add_defaults(tmp_path: Path) -> None:
    env_file = tmp_path / "e.env"
    _run(env_file)
    before = env_file.read_bytes()
    second = _run(env_file)
    assert env_file.read_bytes() == before
    assert "no changes" in second
    assert env_file.read_text().count("RUN_ACTIVE_SECONDS") == 1


def test_existing_overrides_equal_to_old_defaults_are_preserved(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "old.env"
    env_file.write_text("APP_MODE=fixture\n" + STALE, encoding="utf-8")
    _run(env_file)
    text = env_file.read_text()
    assert text.startswith("APP_MODE=fixture\n" + STALE)
    values = local_env.parse_values(text)
    assert values["RUN_ACTIVE_SECONDS"] == "600"
    assert values["EMBEDDING_PROVIDER"] == "hashing"
    snapshot = env_file.read_bytes()
    _run(env_file)
    assert env_file.read_bytes() == snapshot


def test_stale_overrides_are_flagged_with_the_current_default(tmp_path: Path) -> None:
    env_file = tmp_path / "old.env"
    env_file.write_text("APP_MODE=live\n" + STALE, encoding="utf-8")
    output = _run(env_file)
    assert "Possible stale overrides" in output
    assert "RUN_ACTIVE_SECONDS=600" in output and "current default is 120" in output
    assert (
        "EMBEDDING_PROVIDER=hashing" in output and "current default is gemini" in output
    )
    assert "delete the line" in output and "Nothing was changed" in output


def test_old_fixture_mode_is_flagged_and_hashing_in_fixture_is_not(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "old.env"
    env_file.write_text("APP_MODE=fixture\nEMBEDDING_PROVIDER=hashing\n", "utf-8")
    output = _run(env_file)
    assert "APP_MODE=fixture" in output and "current default is live" in output
    # hashing is today's default in fixture mode: removing it changes nothing.
    assert "EMBEDDING_PROVIDER=hashing" not in output


def test_current_values_and_defaults_are_not_flagged(tmp_path: Path) -> None:
    env_file = tmp_path / "ok.env"
    env_file.write_text("RUN_ACTIVE_SECONDS=300\nEMBEDDING_PROVIDER=gemini\n", "utf-8")
    assert "Possible stale overrides" not in _run(env_file)


def test_summary_shows_effective_values_with_their_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fresh = tmp_path / "fresh.env"
    output = _run(fresh)
    assert "APP_MODE            live [default]" in output
    assert "EMBEDDING_PROVIDER  gemini [default]" in output
    assert "RUN_ACTIVE_SECONDS  120 s [default]" in output

    pinned = tmp_path / "pinned.env"
    pinned.write_text("APP_MODE=fixture\nRUN_ACTIVE_SECONDS=600\n", "utf-8")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "gemini")
    output = _run(pinned)
    assert "APP_MODE            fixture [env file]" in output
    assert "RUN_ACTIVE_SECONDS  600 s [env file]" in output
    assert "EMBEDDING_PROVIDER  gemini [environment]" in output


def test_summary_never_prints_secrets(tmp_path: Path) -> None:
    env_file = tmp_path / "s.env"
    output = _run(env_file)
    for value in local_env.secret_values(local_env.parse_values(env_file.read_text())):
        assert value not in output


def test_option_values_are_written_only_when_not_the_default(tmp_path: Path) -> None:
    env_file = tmp_path / "o.env"
    result = CliRunner().invoke(
        local_setup.main,
        [
            "--env-file",
            str(env_file),
            "--env-only",
            "--project",
            "ra-unit",
            "--execution-backend",
            "local",
            "--no-telemetry",
        ],
    )
    assert result.exit_code == 0, result.output
    values = local_env.parse_values(env_file.read_text())
    assert local_env.EXECUTION_BACKEND_KEY not in values  # the default
    assert values[local_env.TELEMETRY_ENABLED_KEY] == "false"  # an explicit choice


def test_registry_entries_are_real_settings_with_a_different_current_default() -> None:
    known = set(config.BACKEND_ENV_NAMES.values())
    for entry in local_env.HISTORICAL_DEFAULTS:
        assert entry.key in known
    keys = {entry.key for entry in local_env.HISTORICAL_DEFAULTS}
    assert {"RUN_ACTIVE_SECONDS", "EMBEDDING_PROVIDER", "APP_MODE"} <= keys
