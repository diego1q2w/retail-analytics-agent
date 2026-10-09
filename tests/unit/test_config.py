from __future__ import annotations

from pathlib import Path

import pytest

from retail_analytics.bootstrap.config import (
    BackendSettings,
    ConfigError,
    RuntimeMode,
    load_backend_settings,
    load_cli_settings,
    missing_api_settings,
)
from retail_analytics.domain.runs import ExecutionBackend

SECRET = "sk-test-do-not-print-0123456789"
LIVE_ENV = {
    "RETAIL_ANALYTICS_MODE": "live",
    "RETAIL_ANALYTICS_DATABASE_URL": f"postgresql://app:{SECRET}@localhost/app",
    "RETAIL_ANALYTICS_TEMPORAL_ADDRESS": "localhost:7233",
    "RETAIL_ANALYTICS_BIGQUERY_PROJECT": "example-project",
    "RETAIL_ANALYTICS_GEMINI_API_KEY": SECRET,
    "RETAIL_ANALYTICS_AUTH_SIGNING_KEY": SECRET + "-signing-key",
}


def test_defaults_to_offline_fixture_mode() -> None:
    settings = load_backend_settings(environ={}, env_file=None)
    assert settings.mode is RuntimeMode.FIXTURE
    assert settings.database_url is None


def test_live_mode_lists_every_missing_required_setting() -> None:
    with pytest.raises(ConfigError) as caught:
        load_backend_settings(environ={"RETAIL_ANALYTICS_MODE": "live"}, env_file=None)
    message = str(caught.value)
    for name in (
        "RETAIL_ANALYTICS_DATABASE_URL",
        "RETAIL_ANALYTICS_BIGQUERY_PROJECT",
        "RETAIL_ANALYTICS_GEMINI_API_KEY",
        "RETAIL_ANALYTICS_AUTH_SIGNING_KEY",
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
        "RETAIL_ANALYTICS_EXECUTION_BACKEND": "temporal",
    }
    with pytest.raises(ConfigError) as caught:
        load_backend_settings(environ=env, env_file=None)
    assert "RETAIL_ANALYTICS_TEMPORAL_ADDRESS" in str(caught.value)
    assert SECRET not in str(caught.value)
    settings = load_backend_settings(
        environ={**env, "RETAIL_ANALYTICS_TEMPORAL_ADDRESS": "localhost:7233"},
        env_file=None,
    )
    assert settings.execution_backend is ExecutionBackend.TEMPORAL


def test_invalid_execution_backend_is_named_without_its_value() -> None:
    with pytest.raises(ConfigError) as caught:
        load_backend_settings(
            environ={"RETAIL_ANALYTICS_EXECUTION_BACKEND": "celery-secret-xyz"},
            env_file=None,
        )
    message = str(caught.value)
    assert "RETAIL_ANALYTICS_EXECUTION_BACKEND" in message
    assert "celery-secret-xyz" not in message


def test_api_requirements_follow_the_execution_backend() -> None:
    local = BackendSettings()
    assert missing_api_settings(local) == [
        "RETAIL_ANALYTICS_DATABASE_URL",
        "RETAIL_ANALYTICS_AUTH_SIGNING_KEY",
    ]
    temporal = BackendSettings(execution_backend=ExecutionBackend.TEMPORAL)
    assert "RETAIL_ANALYTICS_TEMPORAL_ADDRESS" in missing_api_settings(temporal)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("RETAIL_ANALYTICS_LOCAL_MAX_CONCURRENT_RUNS", "0"),
        ("RETAIL_ANALYTICS_LOCAL_MAX_CONCURRENT_RUNS", "65"),
        ("RETAIL_ANALYTICS_LOCAL_SHUTDOWN_GRACE_SECONDS", "-1"),
        ("RETAIL_ANALYTICS_LOCAL_SHUTDOWN_GRACE_SECONDS", "301"),
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
    env = {**LIVE_ENV, "RETAIL_ANALYTICS_GEMINI_API_KEY": ""}
    with pytest.raises(ConfigError, match="RETAIL_ANALYTICS_GEMINI_API_KEY"):
        load_backend_settings(environ=env, env_file=None)


def test_complete_live_settings_validate_and_hide_secrets() -> None:
    settings = load_backend_settings(environ=LIVE_ENV, env_file=None)
    assert settings.mode is RuntimeMode.LIVE
    assert SECRET not in repr(settings)
    assert SECRET not in str(settings.redacted_summary())
    assert settings.redacted_summary()["RETAIL_ANALYTICS_GEMINI_API_KEY"] == "<set>"


def test_invalid_values_are_reported_without_echoing_input() -> None:
    env = {
        **LIVE_ENV,
        "RETAIL_ANALYTICS_API_PORT": SECRET,
        "RETAIL_ANALYTICS_MODE": SECRET,
    }
    with pytest.raises(ConfigError) as caught:
        load_backend_settings(environ=env, env_file=None)
    assert "RETAIL_ANALYTICS_API_PORT" in str(caught.value)
    assert "RETAIL_ANALYTICS_MODE" in str(caught.value)
    assert SECRET not in str(caught.value)
    assert caught.value.__cause__ is None


def test_unknown_prefixed_variable_is_rejected_by_name() -> None:
    with pytest.raises(ConfigError, match="RETAIL_ANALYTICS_GEMINI_KEY"):
        load_backend_settings(
            environ={"RETAIL_ANALYTICS_GEMINI_KEY": SECRET}, env_file=None
        )


def test_env_file_is_overridden_by_process_environment(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "RETAIL_ANALYTICS_API_PORT=9000\nRETAIL_ANALYTICS_API_HOST=0.0.0.0\n"
    )
    settings = load_backend_settings(
        environ={"RETAIL_ANALYTICS_API_PORT": "9100"}, env_file=env_file
    )
    assert settings.api_port == 9100
    assert settings.api_host == "0.0.0.0"  # noqa: S104 - value under test, not a bind


def test_cli_settings_ignore_backend_variables() -> None:
    settings = load_cli_settings(
        environ={**LIVE_ENV, "ANALYTICS_CLI_API_URL": "http://backend:9000"},
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


def test_auth_signing_key_is_secret_and_must_be_long_enough() -> None:
    key = "s" * 40
    settings = load_backend_settings(
        environ={"RETAIL_ANALYTICS_AUTH_SIGNING_KEY": key}, env_file=None
    )
    assert settings.auth_issuer == "retail-analytics-local"
    assert settings.auth_audience == "retail-analytics-api"
    assert key not in repr(settings)
    assert settings.redacted_summary()["RETAIL_ANALYTICS_AUTH_SIGNING_KEY"] == "<set>"

    with pytest.raises(ConfigError) as caught:
        load_backend_settings(
            environ={"RETAIL_ANALYTICS_AUTH_SIGNING_KEY": "short-secret"},
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
    env = {"RETAIL_ANALYTICS_EVIDENCE_CURRENT_FRESHNESS_SECONDS": "120"}
    settings = load_backend_settings(environ=env, env_file=None)
    assert settings.evidence_current_freshness_seconds == 120


@pytest.mark.parametrize("value", ["0", "-5", "59", "86401", "soon"])
def test_evidence_freshness_out_of_range_is_rejected(value: str) -> None:
    env = {"RETAIL_ANALYTICS_EVIDENCE_CURRENT_FRESHNESS_SECONDS": value}
    with pytest.raises(ConfigError) as caught:
        load_backend_settings(environ=env, env_file=None)
    assert "RETAIL_ANALYTICS_EVIDENCE_CURRENT_FRESHNESS_SECONDS" in str(caught.value)


def test_declared_source_currency_is_optional_and_validated() -> None:
    assert (
        load_backend_settings(environ={}, env_file=None).source_currency_declared
        is None
    )
    ok = load_backend_settings(
        environ={"RETAIL_ANALYTICS_SOURCE_CURRENCY_DECLARED": "USD"}, env_file=None
    )
    assert ok.source_currency_declared == "USD"
    for bad in ("usd", "US", "DOLLAR"):
        with pytest.raises(ConfigError) as caught:
            load_backend_settings(
                environ={"RETAIL_ANALYTICS_SOURCE_CURRENCY_DECLARED": bad},
                env_file=None,
            )
        assert "RETAIL_ANALYTICS_SOURCE_CURRENCY_DECLARED" in str(caught.value)
