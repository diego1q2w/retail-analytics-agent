"""Shared test setup: tests never see the developer's real configuration."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from hypothesis import HealthCheck, settings

from retail_analytics.application.telemetry import Telemetry
from retail_analytics.bootstrap.config import BackendSettings

# Stable by default: a fixed seed keeps CI/local runs reproducible, and fuzzing is an
# explicit opt-in (HYPOTHESIS_PROFILE=explore) whose findings become @example cases.
settings.register_profile(
    "default",
    derandomize=True,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile(
    "explore", derandomize=False, deadline=None, max_examples=5000
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "default"))

CONFIG_PREFIXES = ("RETAIL_ANALYTICS_", "ANALYTICS_CLI_")


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Drop prefixed env vars and run from an empty directory (no stray .env)."""
    for key in list(os.environ):
        if key.startswith(CONFIG_PREFIXES):
            monkeypatch.delenv(key)
    # Telemetry is on by default for local runs; tests never export (no network).
    monkeypatch.setenv("RETAIL_ANALYTICS_TELEMETRY_ENABLED", "false")
    monkeypatch.chdir(tmp_path)


@pytest.fixture(autouse=True)
def telemetry_stays_off(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Settings built in code (``BackendSettings()``) default to telemetry on.

    ``build_telemetry`` is wrapped so that, unless a test is marked
    ``real_telemetry``, it always returns the no-op facade. This keeps every
    unit test (and ``./scripts/check.sh``) free of exporter threads and network
    calls whatever settings the test builds.
    """
    if request.node.get_closest_marker("real_telemetry"):
        return
    from retail_analytics.bootstrap import telemetry as composition

    real = composition.build_telemetry

    def build(settings: BackendSettings, service: str) -> Telemetry:
        return real(settings.model_copy(update={"telemetry_enabled": False}), service)

    monkeypatch.setattr(composition, "build_telemetry", build)
