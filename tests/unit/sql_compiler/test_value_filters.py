"""The compiler reports literal comparisons on string fields (value filters)."""

from __future__ import annotations

from retail_analytics.application.contracts.query_compiler import (
    FieldRef,
    ValueFilter,
)
from tests.unit.sql_compiler.support import compile_sql

BRAND = FieldRef("products", "brand")
BASE = (
    "SELECT SUM(s.sale_amount) AS revenue FROM sales_items AS s "
    "JOIN products AS p ON s.product_id = p.product_id "
    "WHERE s.item_status = 'Complete' AND {where}"
)


def _brand_filters(sql: str, **values: str) -> tuple[ValueFilter, ...]:
    compiled = compile_sql(sql, values=values)
    return tuple(f for f in compiled.value_filters if f.field == BRAND)


def test_equality_in_where_is_required() -> None:
    (found,) = _brand_filters(BASE.format(where="p.brand = 'Carhartt'"))
    assert found == ValueFilter(BRAND, ("Carhartt",), required=True)


def test_parameter_values_and_case_wrappers() -> None:
    (found,) = _brand_filters(
        BASE.format(where="LOWER(p.brand) = @brand"), brand="carhartt"
    )
    assert found.values == ("carhartt",)
    assert found.required


def test_in_list_and_like_pattern() -> None:
    (listed,) = _brand_filters(BASE.format(where="p.brand IN ('Alpha', 'Beta')"))
    assert listed.values == ("Alpha", "Beta")
    (pattern,) = _brand_filters(BASE.format(where="p.brand LIKE '%arhar%'"))
    assert pattern.pattern
    assert pattern.values == ("%arhar%",)


def test_alternatives_are_not_required() -> None:
    (found,) = _brand_filters(
        BASE.format(where="(p.brand = 'Alpha' OR p.category = 'Jeans')")
    )
    assert not found.required


def test_comparison_in_a_case_expression_is_not_required() -> None:
    sql = (
        "SELECT SUM(CASE WHEN p.brand = 'Alpha' THEN s.sale_amount END) AS a "
        "FROM sales_items AS s JOIN products AS p ON s.product_id = p.product_id"
    )
    (found,) = _brand_filters(sql)
    assert not found.required


def test_queries_without_string_comparisons_have_none() -> None:
    compiled = compile_sql(
        "SELECT SUM(s.sale_amount) AS revenue FROM sales_items AS s "
        "WHERE s.ordered_date >= DATE '2026-09-01'"
    )
    assert compiled.value_filters == ()
