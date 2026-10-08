"""Offline checks of the source profiling module (no BigQuery)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from retail_analytics.adapters.bigquery.source_profile import (
    ANALYSES,
    FORBIDDEN_TOKENS,
    PROFILE_QUERIES,
    assert_sanitized,
)

ROOT = Path(__file__).resolve().parents[2]


def test_profile_queries_are_aggregates_over_non_personal_columns() -> None:
    for name, sql in PROFILE_QUERIES.items():
        lowered = sql.lower()
        for token in FORBIDDEN_TOKENS:
            assert token not in lowered, (name, token)
        assert "select *" not in lowered, name
        assert "limit" not in lowered or "group by" in lowered, name


def test_sanitizer_rejects_personal_columns_and_emails() -> None:
    with pytest.raises(ValueError):
        assert_sanitized({"x": {"email": "a"}})
    with pytest.raises(ValueError):
        assert_sanitized({"x": "someone@example.com"})
    assert_sanitized({"tables": {"users": {"columns": {"first_name": "STRING"}}}})
    assert_sanitized({"sql": "WHERE s.ordered_date >= @start"})


def test_analyses_use_logical_relations_only() -> None:
    for sql in ANALYSES.values():
        assert "thelook_ecommerce" not in sql


def test_committed_report_is_sanitized_when_present() -> None:
    path = ROOT / "docs/source-profile/source-profile.json"
    if not path.exists():
        pytest.skip("no stored profile report")
    report = json.loads(path.read_text())
    assert_sanitized(report)
    assert report["currency"]["conclusion"] == "UNKNOWN"
    assert report["mapping_check"]["issues"] == []
