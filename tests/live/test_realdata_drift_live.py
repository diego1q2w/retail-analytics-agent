"""Live drift report for the real-data benchmark (separate from its verdict).

Re-runs the reference SQL on the live public tables (about 200 MiB billed) and
compares with the frozen expected values. Drift is the public source changing,
not an agent regression, so it is reported as a skip with the differences, never
as a failure. Skipped without a configured project and credentials.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from retail_analytics.adapters.bigquery.realdata_extract import BigQueryEngine
from retail_analytics.adapters.evaluation import realdata_files as files
from retail_analytics.adapters.google_access import create_bigquery_client
from retail_analytics.application.evaluation.realdata import drift_report
from retail_analytics.bootstrap.config import load_backend_settings

pytestmark = pytest.mark.live
ROOT = Path(__file__).resolve().parents[2]


def test_live_source_against_frozen_expected_values() -> None:
    settings = load_backend_settings(environ={}, env_file=ROOT / ".env")
    if settings.bigquery_project is None:
        pytest.skip("RETAIL_ANALYTICS_BIGQUERY_PROJECT not set")
    try:
        client = create_bigquery_client(
            settings.bigquery_project, settings.bigquery_location
        )
    except Exception:
        pytest.skip("application default credentials not configured")
    spec = files.load_spec(ROOT / "evaluation" / "realdata")
    expected = files.load_expected(ROOT / "evaluation" / "realdata")
    engine = BigQueryEngine(
        client, settings.bigquery_project, settings.bigquery_location
    )
    report = drift_report(
        spec,
        expected,
        engine,
        dataset_ref=engine.dataset_ref(spec),
        observed_at="live-test",
    )
    errors = [e for e in report.entries if e.status == "error"]
    assert not errors, errors
    if report.drifted:
        detail = "; ".join(
            f"{e.query_id}: {', '.join(e.differences[:3])}"
            for e in report.entries
            if e.status == "drift"
        )
        pytest.skip(
            f"live source drifted from the frozen extract (not a regression): {detail}"
        )
