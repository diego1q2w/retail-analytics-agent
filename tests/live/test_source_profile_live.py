"""Live verification of the assumptions the compiler and metrics rely on.

Aggregate queries only (about 20 MiB billed in total); skipped without a
configured project and application default credentials.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from google.cloud import bigquery

from retail_analytics.adapters.bigquery.source_profile import (
    PROFILE_QUERIES,
    check_mappings,
    run_profile,
)
from retail_analytics.adapters.google_access import create_bigquery_client
from retail_analytics.bootstrap.config import load_backend_settings
from retail_analytics.domain.metrics import default_catalog

pytestmark = pytest.mark.live
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def live() -> tuple[str, str, bigquery.Client]:
    settings = load_backend_settings(environ={}, env_file=ROOT / ".env")
    if settings.bigquery_project is None:
        pytest.skip("GOOGLE_CLOUD_PROJECT not set")
    try:
        client = create_bigquery_client(
            settings.bigquery_project, settings.bigquery_location
        )
    except Exception:
        pytest.skip("application default credentials not configured")
    return settings.bigquery_project, settings.bigquery_location, client


@pytest.fixture(scope="module")
def profile(live: tuple[str, str, bigquery.Client]) -> dict[str, Any]:
    project, location, client = live
    result = run_profile(client, project, location)
    assert set(result["profile"]) == set(PROFILE_QUERIES)
    return result


def test_status_values_and_amount_assumptions(profile: dict[str, Any]) -> None:
    statuses = {r["status"] for r in profile["profile"]["order_items_status"]}
    assert "Complete" in statuses  # exact case, as the revenue metric requires
    items = profile["profile"]["order_items"]
    assert items["null_status"] == 0
    assert items["null_sale_price"] == 0
    assert items["rows_total"] == items["distinct_id"]  # one row per item
    assert profile["tables"]["order_items"]["columns"]["sale_price"] == "FLOAT/NULLABLE"


def test_join_keys_are_consistent(profile: dict[str, Any]) -> None:
    p = profile["profile"]
    assert p["orders"]["rows_total"] == p["orders"]["distinct_order_id"]
    assert p["products"]["rows_total"] == p["products"]["distinct_id"]
    assert p["users"]["rows_total"] == p["users"]["distinct_id"]
    assert p["items_vs_orders"]["items_without_order"] == 0
    assert p["items_vs_products"]["items_without_product"] == 0
    assert p["items_vs_users"]["items_without_user"] == 0
    assert p["orders_vs_items"]["orders_without_items"] == 0


def test_order_date_basis_has_no_future_dates(profile: dict[str, Any]) -> None:
    assert profile["profile"]["orders"]["order_created_in_future"] == 0


def test_no_currency_metadata_so_currency_stays_unknown(
    profile: dict[str, Any],
) -> None:
    for table in profile["tables"].values():
        assert not table["currency_named_columns"]
        assert not table["currency_mentioned_in_descriptions"]
    assert default_catalog() is not None


@pytest.mark.asyncio
async def test_catalog_mappings_match_live_metadata(
    live: tuple[str, str, bigquery.Client],
) -> None:
    project, location, _ = live
    assert (await check_mappings(project, location))["issues"] == []
