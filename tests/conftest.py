"""Shared test setup: tests never see the developer's real configuration."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

CONFIG_PREFIXES = ("RETAIL_ANALYTICS_", "ANALYTICS_CLI_")


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Drop prefixed env vars and run from an empty directory (no stray .env)."""
    for key in list(os.environ):
        if key.startswith(CONFIG_PREFIXES):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)
