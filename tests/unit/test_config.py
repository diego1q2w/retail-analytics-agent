from __future__ import annotations

from pathlib import Path

import pytest

from retail_analytics.bootstrap.config import (
    BACKEND_ENV_NAMES,
    CLI_ENV_NAMES,
    BackendSettings,
    ConfigError,
    RuntimeMode,
    legacy_replacement,
    load_backend_settings,
    load_cli_settings,
    missing_api_settings,
)
from retail_analytics.domain.runs import ExecutionBackend

SECRET = "sk-test-do-not-print-0123456789"
LIVE_ENV = {
    "APP_MODE": "live",
    "APP_DATABASE_URL": f"postgresql://app:{SECRET}@localhost/app",
    "TEMPORAL_ADDRESS": "localhost:7233",
    "BIGQUERY_PROJECT": "example-project",
    "GEMINI_API_KEY": SECRET,
    "AUTH_SIGNING_KEY": SECRET + "-signing-key",
}


def test_defaults_to_offline_fixture_mode() -> None:
    settings = load_backend_settings(environ={}, env_file=None)
    assert settings.mode is RuntimeMode.FIXTURE
    assert settings.database_url is None


def test_live_mode_lists_every_missing_required_setting() -> None:
    with pytest.raises(ConfigError) as caught:
        load_backend_settings(environ={"APP_MODE": "live"}, env_file=None)
    message = str(caught.value)
    for name in (
        "APP_DATABASE_URL",
        "BIGQUERY_PROJECT",
        "GEMINI_API_KEY",
        "AUTH_SIGNING_KEY",
    ):
        assert name in message
    # Local execution (the default) never needs Temporal.
    assert "TEMPORAL" not in message


def test_execution_backend_defaults_to_local_independently_of_mode() -> None:
    assert load_backend_settings(environ={}, env_file=None).execution_backend is (
        ExecutionBackend.LOCAL
    )
    live = {k: v for k, v in LIVE_ENV.items() if "TEMPORAL" not in k}
    settings = load_backend_settings(environ=live, env_file=None)
    assert settings.mode is RuntimeMode.LIVE
    assert settings.execution_backend is ExecutionBackend.LOCAL
    assert settings.temporal_address is None


def test_old_temporal_address_alone_does_not_select_temporal() -> None:
    settings = load_backend_settings(environ=LIVE_ENV, env_file=None)
    assert settings.temporal_address == "localhost:7233"
    assert settings.execution_backend is ExecutionBackend.LOCAL


def test_live_temporal_backend_requires_the_temporal_address() -> None:
    env = {
        **{k: v for k, v in LIVE_ENV.items() if "TEMPORAL" not in k},
        "EXECUTION_BACKEND": "temporal",
    }
    with pytest.raises(ConfigError) as caught:
        load_backend_settings(environ=env, env_file=None)
    assert "TEMPORAL_ADDRESS" in str(caught.value)
    assert SECRET not in str(caught.value)
    settings = load_backend_settings(
        environ={**env, "TEMPORAL_ADDRESS": "localhost:7233"},
        env_file=None,
    )
    assert settings.execution_backend is ExecutionBackend.TEMPORAL


def test_invalid_execution_backend_is_named_without_its_value() -> None:
    with pytest.raises(ConfigError) as caught:
        load_backend_settings(
            environ={"EXECUTION_BACKEND": "celery-secret-xyz"},
            env_file=None,
        )
    message = str(caught.value)
    assert "EXECUTION_BACKEND" in message
    assert "celery-secret-xyz" not in message


def test_api_requirements_follow_the_execution_backend() -> None:
    local = BackendSettings()
    assert missing_api_settings(local) == [
        "APP_DATABASE_URL",
        "AUTH_SIGNING_KEY",
    ]
    temporal = BackendSettings(execution_backend=ExecutionBackend.TEMPORAL)
    assert "TEMPORAL_ADDRESS" in missing_api_settings(temporal)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("LOCAL_MAX_CONCURRENT_RUNS", "0"),
        ("LOCAL_MAX_CONCURRENT_RUNS", "65"),
        ("LOCAL_SHUTDOWN_GRACE_SECONDS", "-1"),
        ("LOCAL_SHUTDOWN_GRACE_SECONDS", "301"),
    ],
)
def test_local_execution_limits_are_bounded(name: str, value: str) -> None:
    with pytest.raises(ConfigError, match=name):
        load_backend_settings(environ={name: value}, env_file=None)


def test_local_execution_limit_defaults() -> None:
    settings = BackendSettings()
    assert settings.local_max_concurrent_runs == 4
    assert settings.local_shutdown_grace_seconds == 10.0


def test_empty_values_count_as_missing() -> None:
    env = {**LIVE_ENV, "GEMINI_API_KEY": ""}
    with pytest.raises(ConfigError, match="GEMINI_API_KEY"):
        load_backend_settings(environ=env, env_file=None)


def test_complete_live_settings_validate_and_hide_secrets() -> None:
    settings = load_backend_settings(environ=LIVE_ENV, env_file=None)
    assert settings.mode is RuntimeMode.LIVE
    assert SECRET not in repr(settings)
    assert SECRET not in str(settings.redacted_summary())
    assert settings.redacted_summary()["GEMINI_API_KEY"] == "<set>"


def test_invalid_values_are_reported_without_echoing_input() -> None:
    env = {
        **LIVE_ENV,
        "APP_API_PORT": SECRET,
        "APP_MODE": SECRET,
    }
    with pytest.raises(ConfigError) as caught:
        load_backend_settings(environ=env, env_file=None)
    assert "APP_API_PORT" in str(caught.value)
    assert "APP_MODE" in str(caught.value)
    assert SECRET not in str(caught.value)
    assert caught.value.__cause__ is None


def test_process_environment_is_read_only_for_known_names() -> None:
    # A stray, unrelated variable in the shell is ignored, never an error.
    settings = load_backend_settings(
        environ={"GEMINI_KEY": SECRET, "PAGER": "less", "APP_API_PORT": "9001"},
        env_file=None,
    )
    assert settings.api_port == 9001


def test_unknown_env_file_key_is_flagged_without_its_value(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"GEMINI_KEY={SECRET}\nCOMPOSE_POSTGRES_PORT=55442\nAPP_MODE=fixture\n"
    )
    for load in (load_backend_settings, load_cli_settings):
        with pytest.raises(ConfigError) as caught:
            load(environ={}, env_file=env_file)
        message = str(caught.value)
        assert "unknown variable GEMINI_KEY" in message
        assert "did you mean GEMINI_API_KEY?" in message
        assert "COMPOSE_" not in message
        assert SECRET not in message


@pytest.mark.parametrize(
    ("legacy", "current"),
    [
        ("RETAIL_ANALYTICS_GEMINI_API_KEY", "GEMINI_API_KEY"),
        ("RETAIL_ANALYTICS_MODE", "APP_MODE"),
        ("RETAIL_ANALYTICS_EXECUTION_BACKEND", "EXECUTION_BACKEND"),
        ("ANALYTICS_CLI_TOKEN", "CLI_TOKEN"),
    ],
)
def test_legacy_names_in_env_file_are_refused_with_the_fix(
    tmp_path: Path, legacy: str, current: str
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(f"{legacy}={SECRET}\n")
    for load in (load_backend_settings, load_cli_settings):
        with pytest.raises(ConfigError) as caught:
            load(environ={}, env_file=env_file)
        message = str(caught.value)
        assert f"{legacy} -> {current}" in message
        assert "./scripts/bootstrap.sh --env-only" in message
        assert SECRET not in message


def test_legacy_names_in_process_environment_are_refused() -> None:
    with pytest.raises(ConfigError) as caught:
        load_backend_settings(
            environ={"RETAIL_ANALYTICS_DATABASE_URL": SECRET}, env_file=None
        )
    message = str(caught.value)
    assert "RETAIL_ANALYTICS_DATABASE_URL -> APP_DATABASE_URL" in message
    assert "Unset them" in message
    assert SECRET not in message
    with pytest.raises(ConfigError, match="ANALYTICS_CLI_API_URL -> CLI_API_URL"):
        load_cli_settings(environ={"ANALYTICS_CLI_API_URL": "http://x"}, env_file=None)


def test_pointed_env_file_with_legacy_names_names_it_in_the_fix(
    tmp_path: Path,
) -> None:
    pointed = tmp_path / "alt.env"
    pointed.write_text("RETAIL_ANALYTICS_API_PORT=9000\n")
    with pytest.raises(ConfigError) as caught:
        load_backend_settings(environ={"APP_ENV_FILE": str(pointed)})
    assert f"--env-only --env-file {pointed}" in str(caught.value)


def test_legacy_replacement_covers_every_setting() -> None:
    for field, name in BACKEND_ENV_NAMES.items():
        assert legacy_replacement("RETAIL_ANALYTICS_" + field.upper()) == name
    for field, name in CLI_ENV_NAMES.items():
        assert legacy_replacement("ANALYTICS_CLI_" + field.upper()) == name
    assert legacy_replacement("RETAIL_ANALYTICS_ENV_FILE") == "APP_ENV_FILE"
    assert legacy_replacement("COMPOSE_POSTGRES_PORT") is None
    assert legacy_replacement("APP_DATABASE_URL") is None
    names = [*BACKEND_ENV_NAMES.values(), *CLI_ENV_NAMES.values()]
    assert len(set(names)) == len(names)
    assert not any(n.startswith(("RETAIL_ANALYTICS_", "ANALYTICS_CLI_")) for n in names)


def test_env_file_is_overridden_by_process_environment(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("APP_API_PORT=9000\nAPP_API_HOST=0.0.0.0\n")
    settings = load_backend_settings(
        environ={"APP_API_PORT": "9100"}, env_file=env_file
    )
    assert settings.api_port == 9100
    assert settings.api_host == "0.0.0.0"  # noqa: S104 - value under test, not a bind


def test_cli_settings_ignore_backend_variables() -> None:
    settings = load_cli_settings(
        environ={**LIVE_ENV, "CLI_API_URL": "http://backend:9000"},
        env_file=None,
    )
    assert settings.api_url == "http://backend:9000"


def test_settings_are_immutable() -> None:
    settings = BackendSettings()
    with pytest.raises(ValueError, match="frozen"):
        settings.api_port = 1


def test_env_example_matches_settings() -> None:
    example = Path(__file__).resolve().parents[2] / ".env.example"
    backend = load_backend_settings(environ={}, env_file=example)
    assert backend.mode is RuntimeMode.FIXTURE
    assert load_cli_settings(environ={}, env_file=example).timeout_seconds == 10
    documented = {
        line.split("=", 1)[0]
        for line in example.read_text().splitlines()
        if line and not line.startswith("#")
    }
    assert set(backend.redacted_summary()) <= documented
    assert set(CLI_ENV_NAMES.values()) <= documented


def test_auth_signing_key_is_secret_and_must_be_long_enough() -> None:
    key = "s" * 40
    settings = load_backend_settings(environ={"AUTH_SIGNING_KEY": key}, env_file=None)
    assert settings.auth_issuer == "retail-analytics-local"
    assert settings.auth_audience == "retail-analytics-api"
    assert key not in repr(settings)
    assert settings.redacted_summary()["AUTH_SIGNING_KEY"] == "<set>"

    with pytest.raises(ConfigError) as caught:
        load_backend_settings(
            environ={"AUTH_SIGNING_KEY": "short-secret"},
            env_file=None,
        )
    assert "AUTH_SIGNING_KEY must be at least 32 bytes" in str(caught.value)
    assert "short-secret" not in str(caught.value)


def test_evidence_freshness_default_and_override() -> None:
    assert (
        load_backend_settings(
            environ={}, env_file=None
        ).evidence_current_freshness_seconds
        == 900
    )
    env = {"EVIDENCE_CURRENT_FRESHNESS_SECONDS": "120"}
    settings = load_backend_settings(environ=env, env_file=None)
    assert settings.evidence_current_freshness_seconds == 120


@pytest.mark.parametrize("value", ["0", "-5", "59", "86401", "soon"])
def test_evidence_freshness_out_of_range_is_rejected(value: str) -> None:
    env = {"EVIDENCE_CURRENT_FRESHNESS_SECONDS": value}
    with pytest.raises(ConfigError) as caught:
        load_backend_settings(environ=env, env_file=None)
    assert "EVIDENCE_CURRENT_FRESHNESS_SECONDS" in str(caught.value)


def test_declared_source_currency_is_optional_and_validated() -> None:
    assert (
        load_backend_settings(environ={}, env_file=None).source_currency_declared
        is None
    )
    ok = load_backend_settings(
        environ={"SOURCE_CURRENCY_DECLARED": "USD"}, env_file=None
    )
    assert ok.source_currency_declared == "USD"
    for bad in ("usd", "US", "DOLLAR"):
        with pytest.raises(ConfigError) as caught:
            load_backend_settings(
                environ={"SOURCE_CURRENCY_DECLARED": bad},
                env_file=None,
            )
        assert "SOURCE_CURRENCY_DECLARED" in str(caught.value)


@pytest.mark.parametrize(
    ("bare", "expected"),
    [("MODE", "APP_MODE"), ("API_PORT", "APP_API_PORT"), ("ENV_FILE", "APP_ENV_FILE")],
)
def test_hand_renamed_bare_names_point_to_the_expected_key(
    tmp_path: Path, bare: str, expected: str
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(f"{bare}={SECRET}\n")
    with pytest.raises(ConfigError) as caught:
        load_backend_settings(environ={}, env_file=env_file)
    message = str(caught.value)
    assert f"unknown variable {bare} (did you mean {expected}?)" in message
    assert SECRET not in message


def test_database_url_is_app_scoped_and_bare_form_is_ignored(tmp_path: Path) -> None:
    url = f"postgresql://app:{SECRET}@localhost/app"
    # A shell DATABASE_URL from another project never reaches the settings.
    settings = load_backend_settings(environ={"DATABASE_URL": url}, env_file=None)
    assert settings.database_url is None
    assert legacy_replacement("RETAIL_ANALYTICS_DATABASE_URL") == "APP_DATABASE_URL"
    env_file = tmp_path / ".env"
    env_file.write_text(f"DATABASE_URL={url}\n")
    with pytest.raises(ConfigError) as caught:
        load_backend_settings(environ={}, env_file=env_file)
    message = str(caught.value)
    assert "unknown variable DATABASE_URL (did you mean APP_DATABASE_URL?)" in message
    assert SECRET not in message
