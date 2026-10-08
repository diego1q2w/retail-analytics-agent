"""Shared test setup: tests never see the developer's real configuration."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from hypothesis import HealthCheck, settings

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
    monkeypatch.chdir(tmp_path)
