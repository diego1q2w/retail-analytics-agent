"""The compiler's date window: exact when unambiguous, otherwise None."""

from __future__ import annotations

from datetime import date

import pytest

from retail_analytics.domain.periods import DateWindow
from tests.unit.sql_compiler.support import compile_sql

SEPTEMBER = DateWindow(date(2026, 9, 1), date(2026, 10, 1))
REVENUE = (
    "SELECT SUM(s.sale_amount) AS revenue FROM sales_items AS s "
    "WHERE s.item_status = 'Complete' AND {where}"
)


@pytest.mark.parametrize(
    "where",
    [
        "s.ordered_date >= DATE '2026-09-01' AND s.ordered_date < DATE '2026-10-01'",
        "s.ordered_date BETWEEN DATE '2026-09-01' AND DATE '2026-09-30'",
        "DATE '2026-09-01' <= s.ordered_date AND s.ordered_date <= DATE '2026-09-30'",
        "s.ordered_date > DATE '2026-08-31' AND s.ordered_date < DATE '2026-10-01'",
    ],
)
def test_simple_windows_are_exact(where: str) -> None:
    assert compile_sql(REVENUE.format(where=where)).date_window == SEPTEMBER


def test_model_supplied_date_values() -> None:
    compiled = compile_sql(
        REVENUE.format(where="s.ordered_date >= @start AND s.ordered_date < @end"),
        values={"start": date(2026, 9, 1), "end": date(2026, 10, 1)},
    )
    assert compiled.date_window == SEPTEMBER


def test_window_inside_a_cte() -> None:
    sql = (
        "WITH sept AS (SELECT s.product_id, s.sale_amount FROM sales_items AS s "
        "WHERE s.item_status = 'Complete' AND s.ordered_date >= DATE '2026-09-01' "
        "AND s.ordered_date < DATE '2026-10-01') "
        "SELECT sept.product_id, SUM(sept.sale_amount) AS revenue FROM sept "
        "GROUP BY sept.product_id"
    )
    assert compile_sql(sql).date_window == SEPTEMBER


@pytest.mark.parametrize(
    "sql",
    [
        # No date filter: all time, not a period.
        "SELECT SUM(s.sale_amount) AS revenue FROM sales_items AS s",
        # Only one bound.
        REVENUE.format(where="s.ordered_date >= DATE '2026-09-01'"),
        # Under OR: not a single window.
        REVENUE.format(
            where="(s.ordered_date < DATE '2026-09-01' "
            "OR s.ordered_date >= DATE '2026-10-01')"
        ),
        # Two periods compared in one query.
        "SELECT SUM(CASE WHEN s.ordered_date >= DATE '2026-09-01' "
        "THEN s.sale_amount ELSE 0 END) AS sept, SUM(s.sale_amount) AS total "
        "FROM sales_items AS s WHERE s.ordered_date >= DATE '2026-08-01' "
        "AND s.ordered_date < DATE '2026-10-01' AND s.ordered_date < DATE '2026-09-15'",
        # A second dated read with a different window.
        "SELECT (SELECT SUM(s.sale_amount) FROM sales_items AS s "
        "WHERE s.ordered_date >= DATE '2026-09-01' "
        "AND s.ordered_date < DATE '2026-10-01') AS sept, "
        "(SELECT SUM(t.sale_amount) FROM sales_items AS t "
        "WHERE t.ordered_date >= DATE '2026-08-01' "
        "AND t.ordered_date < DATE '2026-09-01') AS aug",
    ],
)
def test_ambiguous_or_missing_windows_are_not_recorded(sql: str) -> None:
    assert compile_sql(sql).date_window is None
