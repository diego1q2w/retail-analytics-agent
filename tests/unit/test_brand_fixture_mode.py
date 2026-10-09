"""Fixture mode provisions a deterministic synthetic brand catalog.

The demo brand managers get the held-out fixture's synthetic brands, so
brand-dependent scenarios return a real permitted result and a real
cross-brand denial instead of both managers seeing nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlglot

from retail_analytics.adapters.evaluation.fixture_warehouse import (
    FixtureWarehouse,
    HeldoutProductBrands,
    heldout_fixture_warehouse,
)
from retail_analytics.application.brand_access import ProductBrandsUnavailable
from retail_analytics.application.query_compiler import QueryRejected
from retail_analytics.bootstrap.access import product_brand_source
from retail_analytics.bootstrap.config import RuntimeMode, load_backend_settings
from retail_analytics.bootstrap.dev_access import DEMO_EXECUTIVES, LOCAL_ADMIN
from tests.unit.sql_compiler.support import compile_sql, scope

FIXTURE = Path(__file__).resolve().parents[2] / "evaluation/heldout/fixture"
DEMO_A, DEMO_B = (d.for_mode(RuntimeMode.FIXTURE) for d in DEMO_EXECUTIVES)
SALES = (
    "SELECT product_id, SUM(sale_amount) AS total FROM sales_items "
    "WHERE item_status = 'Complete' GROUP BY product_id ORDER BY product_id"
)


async def _products_of(brands: frozenset[str]) -> frozenset[str]:
    catalog = await HeldoutProductBrands(FIXTURE).read_product_brands()
    return frozenset(p for p, b in catalog.brands.items() if b in brands)


def _sold_products(warehouse: FixtureWarehouse, products: frozenset[str]) -> set[int]:
    compiled = compile_sql(SALES, scope(*(int(p) for p in products)))
    sql = sqlglot.parse_one(compiled.sql, read="bigquery").sql(dialect="duckdb")
    params = {p.name: p.value for p in compiled.parameters if f"${p.name}" in sql}
    rows = warehouse._db.execute(sql, params).fetchall()
    return {int(row[0]) for row in rows}


def test_fixture_mode_uses_synthetic_brands_and_live_mode_keeps_real_ones() -> None:
    assert DEMO_A.brands == {"Aster", "Birch"}
    assert DEMO_B.brands == {"Cedar", "Dune"}
    live = [d.for_mode(RuntimeMode.LIVE) for d in DEMO_EXECUTIVES]
    assert live[0].brands == {"Calvin Klein", "Levi's"}
    assert live[1].brands == {"Carhartt", "Columbia"}
    assert LOCAL_ADMIN.for_mode(RuntimeMode.FIXTURE) is LOCAL_ADMIN


@pytest.mark.asyncio
async def test_each_manager_gets_a_permitted_result_and_is_denied_the_other() -> None:
    warehouse = heldout_fixture_warehouse(FIXTURE)
    a_products = await _products_of(DEMO_A.brands)
    b_products = await _products_of(DEMO_B.brands)
    assert a_products == {"201", "202", "203", "204"}
    assert b_products == {"205", "206", "207"}

    # Permitted: real, non-empty rows, only for the manager's own products.
    a_sold = _sold_products(warehouse, a_products)
    b_sold = _sold_products(warehouse, b_products)
    assert a_sold == {201, 202, 203, 204}
    assert b_sold == {205, 206}  # 207 has no completed sales

    # Cross-brand: each manager's scope never reaches the other's products.
    assert not a_sold & {205, 206, 207}
    assert not b_sold & {201, 202, 203, 204}


@pytest.mark.asyncio
async def test_a_manager_with_no_synced_products_is_refused_not_answered() -> None:
    # The failure mode this task removes: an unsynced catalog must be loud.
    with pytest.raises(QueryRejected):
        compile_sql(SALES, scope())


@pytest.mark.asyncio
async def test_missing_fixture_files_give_an_actionable_error(tmp_path: Path) -> None:
    with pytest.raises(ProductBrandsUnavailable, match="repository root"):
        await HeldoutProductBrands(tmp_path).read_product_brands()


def test_fixture_mode_has_a_brand_source() -> None:
    env = {"APP_MODE": "fixture", "APP_DATABASE_URL": "postgresql://x@h/db"}
    assert product_brand_source(load_backend_settings(env)) is not None
