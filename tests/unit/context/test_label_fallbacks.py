"""Missing product names and brands: explicit display fallbacks, never merged."""

from __future__ import annotations

from dataclasses import replace

import pytest

from retail_analytics.domain.labels import (
    UNKNOWN_BRAND,
    UNNAMED_PRODUCT,
    format_cell,
    label_notes,
)
from tests.unit.context.support import A, World
from tests.unit.privacy.support import EXEC_A
from tests.unit.sql_compiler.support import RAW

pytestmark = pytest.mark.asyncio

BY_BRAND = (
    "SELECT p.brand, SUM(s.sale_amount) AS revenue FROM sales_items s "
    "JOIN products p ON s.product_id = p.product_id GROUP BY p.brand"
)
BY_PRODUCT = (
    "SELECT s.product_id, p.product_name, SUM(s.sale_amount) AS revenue "
    "FROM sales_items s JOIN products p ON s.product_id = p.product_id "
    "GROUP BY s.product_id, p.product_name"
)
BY_NAME_ONLY = (
    "SELECT p.product_name, SUM(s.sale_amount) AS revenue FROM sales_items s "
    "JOIN products p ON s.product_id = p.product_id GROUP BY p.product_name"
)
TOTAL = "SELECT SUM(s.sale_amount) AS total FROM sales_items s"


def world_with_unlabelled_products() -> World:
    """Products 4 and 5 have no name; 4 and 5 have no brand either."""
    w = World()
    w.db.execute(
        f"INSERT INTO {RAW}products VALUES "  # noqa: S608
        "(4,NULL,'Clothing',NULL,'Women',12,4),(5,NULL,'Clothing',NULL,'Men',18,6)"
    )
    w.db.execute(
        f"INSERT INTO {RAW}order_items VALUES "  # noqa: S608
        "(1010,100,10,4,'Complete',11),(1011,102,20,5,'Complete',22)"
    )
    current = w.directory.by_id[EXEC_A]
    w.directory.by_id[EXEC_A] = replace(
        current, product_ids=frozenset({"1", "2", "3", "4", "5"})
    )
    return w


def lines_of(rendered: str, evidence_id: str) -> list[str]:
    block = rendered.split(f"evidence {evidence_id}")[1]
    return [line for line in block.splitlines()[1:] if line]


async def test_two_unnamed_products_stay_two_rows_with_their_ids() -> None:
    w = world_with_unlabelled_products()
    r1 = w.new_run()
    evidence, released = await w.query(r1, BY_PRODUCT, grain=("product_id",))
    by_id = {str(r["product_id"]): r["product_name"] for r in released.records()}
    assert by_id["4"] is None and by_id["5"] is None

    r2 = w.new_run()
    context = await w.builder.build(A, r2, "Which products have no name?")
    shown = [r for e in context.evidence for r in e.rows]
    unnamed = [r for r in shown if r[1] == UNNAMED_PRODUCT]
    assert sorted(r[0] for r in unnamed) == ["4", "5"]
    assert len(context.render().split(UNNAMED_PRODUCT + " |")) - 1 == 2
    stored = w.store.records[evidence.evidence_id].content.table
    assert [row[1] for row in stored.rows if row[0] in (4, 5)] == [None, None]
    assert w.store.records[evidence.evidence_id].is_intact
    assert any(
        "2 row(s)" in n and UNNAMED_PRODUCT in n for n in context.evidence[0].notes
    )


async def test_name_only_breakdowns_warn_that_unnamed_products_are_not_distinct() -> (
    None
):
    w = world_with_unlabelled_products()
    r1 = w.new_run()
    await w.query(r1, BY_NAME_ONLY, grain=("product_name",))
    context = await w.builder.build(A, w.new_run(), "By name")
    notes = context.evidence[0].notes
    assert any("cannot be told apart" in n for n in notes)
    assert "note: " in context.render()


async def test_unknown_brand_is_its_own_group_and_totals_reconcile() -> None:
    w = world_with_unlabelled_products()
    r1 = w.new_run()
    by_brand, released = await w.query(r1, BY_BRAND, grain=("brand",))
    total, total_released = await w.query(r1, TOTAL, grain=())
    grouped = {r["brand"]: float(str(r["revenue"])) for r in released.records()}
    assert set(grouped) == {"Alpha", "Beta", None}  # NULL kept, not dropped or merged
    assert grouped[None] == 33  # products 4 and 5 together
    assert sum(grouped.values()) == float(str(total_released.records()[0]["total"]))

    context = await w.builder.build(A, w.new_run(), "Revenue by brand")
    digest = next(e for e in context.evidence if e.columns[0] == "brand")
    assert sorted(r[0] for r in digest.rows) == sorted(["Alpha", "Beta", UNKNOWN_BRAND])
    assert any("1 row(s)" in n and UNKNOWN_BRAND in n for n in digest.notes)
    stored = w.store.records[by_brand.evidence_id].content.table
    assert None in [row[0] for row in stored.rows]
    assert total.evidence_id != by_brand.evidence_id


async def test_presentation_helpers_apply_fallbacks_only_to_null_label_cells() -> None:
    w = world_with_unlabelled_products()
    evidence, _ = await w.query(w.new_run(), BY_PRODUCT, grain=("product_id",))
    table = evidence.content.table
    assert label_notes(table)
    name_column = next(c for c in table.columns if c.name == "product_name")
    assert format_cell(None, "Unnamed product") == "Unnamed product"
    assert format_cell(None) == "(none)"
    assert name_column.sources == ("products.product_name",)
