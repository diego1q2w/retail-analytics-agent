"""Live Google access checks. Skipped unless real credentials are configured.

Reads the developer's ignored ``.env`` through the typed loader; never prints it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from retail_analytics.application.access_check import check_model, check_warehouse
from retail_analytics.bootstrap.check_credentials import (
    build_model,
    build_warehouse,
)
from retail_analytics.bootstrap.config import BackendSettings, load_backend_settings

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.live


@pytest.fixture
def live_settings() -> BackendSettings:
    return load_backend_settings(environ={}, env_file=ROOT / ".env")


def test_bigquery_access(live_settings: BackendSettings) -> None:
    warehouse = build_warehouse(live_settings)
    if warehouse is None:
        pytest.skip("RETAIL_ANALYTICS_BIGQUERY_PROJECT not set")
    results = check_warehouse(warehouse)
    if results[0].name == "bigquery credentials" and not results[0].ok:
        pytest.skip("application default credentials not configured")
    failures = [f"{r.name}: {r.detail}" for r in results if not r.ok]
    assert not failures, failures


def test_gemini_access(live_settings: BackendSettings) -> None:
    model = build_model(live_settings)
    if model is None:
        pytest.skip("RETAIL_ANALYTICS_GEMINI_API_KEY not set")
    result = check_model(model)
    assert result.ok, f"{result.detail} ({result.remedy})"
