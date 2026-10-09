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


# --- calendar month via EXTRACT (T08-F1) ---------------------------------------------

LATEST = (
    "SELECT MIN(s.ordered_date) AS first_day, MAX(s.ordered_date) AS last_day, "
    "SUM(s.sale_amount) AS revenue FROM sales_items AS s "
    "WHERE s.item_status = 'Complete' "
    "AND EXTRACT(MONTH FROM s.ordered_date) = @month "
    "AND EXTRACT(YEAR FROM s.ordered_date) = "
    "(SELECT MAX(EXTRACT(YEAR FROM x.ordered_date)) FROM sales_items AS x "
    "WHERE EXTRACT(MONTH FROM x.ordered_date) = @month)"
)


@pytest.mark.parametrize(
    ("where", "values"),
    [
        (
            "EXTRACT(YEAR FROM s.ordered_date) = 2026 "
            "AND EXTRACT(MONTH FROM s.ordered_date) = 9",
            {},
        ),
        (
            "EXTRACT(MONTH FROM s.ordered_date) = @month "
            "AND EXTRACT(YEAR FROM s.ordered_date) = @year",
            {"month": 9, "year": 2026},
        ),
        (
            "2026 = EXTRACT(YEAR FROM s.ordered_date) "
            "AND 9 = EXTRACT(MONTH FROM s.ordered_date)",
            {},
        ),
        # The same constraint twice is still one month.
        (
            "EXTRACT(YEAR FROM s.ordered_date) = 2026 "
            "AND EXTRACT(MONTH FROM s.ordered_date) = 9 "
            "AND EXTRACT(MONTH FROM s.ordered_date) = @month",
            {"month": 9},
        ),
    ],
)
def test_extract_year_and_month_is_one_calendar_month(
    where: str, values: dict[str, int]
) -> None:
    compiled = compile_sql(REVENUE.format(where=where), values=values)
    assert compiled.date_window == SEPTEMBER
    assert compiled.latest_month is None


@pytest.mark.parametrize(
    ("year", "days"), [(2024, 29), (2025, 28), (2000, 29), (2100, 28)]
)
def test_february_follows_the_calendar(year: int, days: int) -> None:
    compiled = compile_sql(
        REVENUE.format(
            where="EXTRACT(YEAR FROM s.ordered_date) = @y "
            "AND EXTRACT(MONTH FROM s.ordered_date) = @m"
        ),
        values={"y": year, "m": 2},
    )
    assert compiled.date_window == DateWindow(date(year, 2, 1), date(year, 3, 1))
    assert compiled.date_window.days == days


def test_december_ends_at_the_next_new_year() -> None:
    compiled = compile_sql(
        REVENUE.format(
            where="EXTRACT(YEAR FROM s.ordered_date) = 2025 "
            "AND EXTRACT(MONTH FROM s.ordered_date) = 12"
        )
    )
    assert compiled.date_window == DateWindow(date(2025, 12, 1), date(2026, 1, 1))


def test_extract_month_inside_a_cte() -> None:
    sql = (
        "WITH sept AS (SELECT s.product_id, s.sale_amount FROM sales_items AS s "
        "WHERE EXTRACT(YEAR FROM s.ordered_date) = 2026 "
        "AND EXTRACT(MONTH FROM s.ordered_date) = 9) "
        "SELECT sept.product_id, SUM(sept.sale_amount) AS revenue FROM sept "
        "GROUP BY sept.product_id"
    )
    assert compile_sql(sql).date_window == SEPTEMBER


Y26 = "EXTRACT(YEAR FROM s.ordered_date) = 2026"
M9 = "EXTRACT(MONTH FROM s.ordered_date) = 9"


@pytest.mark.parametrize(
    ("sql", "values"),
    [
        # Month alone spans every year; year alone is not a single month.
        (REVENUE.format(where=M9), {}),
        (REVENUE.format(where=Y26), {}),
        # Conflicting filters.
        (
            REVENUE.format(
                where=f"{Y26} AND {M9} AND EXTRACT(MONTH FROM s.ordered_date) = 10"
            ),
            {},
        ),
        (
            REVENUE.format(
                where=f"{Y26} AND {M9} AND EXTRACT(YEAR FROM s.ordered_date) = 2025"
            ),
            {},
        ),
        # OR / NOT.
        (REVENUE.format(where=f"({Y26} OR {M9})"), {}),
        (
            REVENUE.format(
                where=f"{Y26} AND ({M9} OR EXTRACT(MONTH FROM s.ordered_date) = 10)"
            ),
            {},
        ),
        (REVENUE.format(where=f"{Y26} AND NOT ({M9})"), {}),
        (
            REVENUE.format(
                where=f"{Y26} AND NOT EXTRACT(MONTH FROM s.ordered_date) = 9"
            ),
            {},
        ),
        # Other comparisons or units.
        (
            REVENUE.format(where=f"{Y26} AND EXTRACT(MONTH FROM s.ordered_date) >= 9"),
            {},
        ),
        (
            REVENUE.format(
                where=f"{Y26} AND EXTRACT(MONTH FROM s.ordered_date) IN (9)"
            ),
            {},
        ),
        (
            REVENUE.format(
                where=f"{Y26} AND {M9} AND EXTRACT(DAY FROM s.ordered_date) = 1"
            ),
            {},
        ),
        # Mixed with range bounds: not combined.
        (
            REVENUE.format(
                where=f"{Y26} AND {M9} AND s.ordered_date >= DATE '2026-09-15'"
            ),
            {},
        ),
        # Impossible month.
        (
            REVENUE.format(where=f"{Y26} AND EXTRACT(MONTH FROM s.ordered_date) = 13"),
            {},
        ),
        # Untyped or wrongly typed values.
        (
            REVENUE.format(where=f"{Y26} AND EXTRACT(MONTH FROM s.ordered_date) = @m"),
            {"m": "9"},
        ),
        (
            REVENUE.format(where=f"{Y26} AND EXTRACT(MONTH FROM s.ordered_date) = @m"),
            {"m": 9.0},
        ),
        (
            REVENUE.format(where=f"{Y26} AND EXTRACT(MONTH FROM s.ordered_date) = @m"),
            {"m": True},
        ),
        # Year and month on different dated relations (alias mismatch).
        (
            "SELECT SUM(s.sale_amount) AS r FROM sales_items AS s JOIN orders AS o "
            "ON s.order_ref = o.order_ref "
            "WHERE EXTRACT(YEAR FROM o.ordered_date) = 2026 "
            "AND EXTRACT(MONTH FROM s.ordered_date) = 9",
            {},
        ),
        # A filter in HAVING on the date.
        (
            "SELECT s.product_id, SUM(s.sale_amount) AS r FROM sales_items AS s "
            f"WHERE {Y26} AND {M9} GROUP BY s.product_id, s.ordered_date "
            "HAVING EXTRACT(DAY FROM s.ordered_date) > 1",
            {},
        ),
        # A second dated read in another month.
        (
            "SELECT (SELECT SUM(s.sale_amount) FROM sales_items AS s "
            f"WHERE {Y26} AND {M9}) AS sept, "
            "(SELECT SUM(t.sale_amount) FROM sales_items AS t "
            "WHERE EXTRACT(YEAR FROM t.ordered_date) = 2026 "
            "AND EXTRACT(MONTH FROM t.ordered_date) = 8) AS aug",
            {},
        ),
        # The month filters a derived column, not the source date.
        (
            "WITH d AS (SELECT s.ordered_date AS day, s.sale_amount "
            "FROM sales_items AS s) SELECT SUM(d.sale_amount) AS r FROM d "
            "WHERE EXTRACT(YEAR FROM d.day) = 2026 AND EXTRACT(MONTH FROM d.day) = 9",
            {},
        ),
    ],
)
def test_unsupported_month_shapes_record_no_window(
    sql: str, values: dict[str, object]
) -> None:
    compiled = compile_sql(sql, values=values)  # type: ignore[arg-type]
    assert compiled.date_window is None
    assert compiled.latest_month is None


# --- latest occurrence of a month (year chosen by a scalar subquery) ------------------


def test_latest_month_pattern_names_its_witnesses() -> None:
    compiled = compile_sql(LATEST, values={"month": 9})
    assert compiled.date_window is None  # the year is known only after execution
    assert compiled.latest_month is not None
    assert compiled.latest_month.month == 9
    assert compiled.latest_month.witnesses == ("first_day", "last_day")


def test_documented_example_is_the_recognized_shape() -> None:
    from retail_analytics.application.contracts.sql_dialect import (
        LATEST_MONTH_EXAMPLE,
    )

    latest = compile_sql(LATEST_MONTH_EXAMPLE, values={"month": 2}).latest_month
    assert latest is not None and latest.month == 2


def test_latest_month_with_a_literal_month_and_grouped_date() -> None:
    sql = (
        "SELECT s.ordered_date AS day, SUM(s.sale_amount) AS revenue "
        "FROM sales_items AS s WHERE EXTRACT(MONTH FROM s.ordered_date) = 2 "
        "AND (SELECT MAX(EXTRACT(YEAR FROM x.ordered_date)) FROM sales_items AS x) "
        "= EXTRACT(YEAR FROM s.ordered_date) GROUP BY s.ordered_date"
    )
    latest = compile_sql(sql).latest_month
    assert latest is not None
    assert (latest.month, latest.witnesses) == (2, ("day",))


_SUB = (
    "(SELECT MAX(EXTRACT(YEAR FROM x.ordered_date)) FROM sales_items AS x "
    "WHERE EXTRACT(MONTH FROM x.ordered_date) = 9)"
)
_REV = "SELECT MAX(s.ordered_date) AS last_day, SUM(s.sale_amount) AS r "


@pytest.mark.parametrize(
    "sql",
    [
        # No output carries the filtered dates: the year cannot be verified.
        "SELECT SUM(s.sale_amount) AS r FROM sales_items AS s "
        f"WHERE {M9} AND EXTRACT(YEAR FROM s.ordered_date) = {_SUB}",
        # The witness is computed, not the date itself.
        "SELECT DATE_ADD(MAX(s.ordered_date), INTERVAL 1 DAY) AS d, "
        "SUM(s.sale_amount) AS r FROM sales_items AS s "
        f"WHERE {M9} AND EXTRACT(YEAR FROM s.ordered_date) = {_SUB}",
        # Without the month filter the year subquery selects a whole year.
        _REV
        + f"FROM sales_items AS s WHERE EXTRACT(YEAR FROM s.ordered_date) = {_SUB}",
        # Extra date filters, OR and NOT.
        _REV + f"FROM sales_items AS s WHERE {M9} AND "
        f"EXTRACT(YEAR FROM s.ordered_date) = {_SUB} "
        "AND s.ordered_date >= DATE '2026-09-15'",
        _REV + f"FROM sales_items AS s WHERE {M9} AND "
        f"(EXTRACT(YEAR FROM s.ordered_date) = {_SUB} OR {Y26})",
        _REV + f"FROM sales_items AS s WHERE {M9} AND "
        f"NOT (EXTRACT(YEAR FROM s.ordered_date) = {_SUB})",
        _REV + f"FROM sales_items AS s WHERE {M9} AND "
        f"EXTRACT(YEAR FROM s.ordered_date) = {_SUB} "
        "AND EXTRACT(MONTH FROM s.ordered_date) = 10",
        # The subquery is not MAX(EXTRACT(YEAR ...)) of a dated relation.
        _REV + f"FROM sales_items AS s WHERE {M9} AND "
        "EXTRACT(YEAR FROM s.ordered_date) = "
        "(SELECT MIN(EXTRACT(YEAR FROM x.ordered_date)) FROM sales_items AS x)",
        _REV + f"FROM sales_items AS s WHERE {M9} AND "
        "EXTRACT(YEAR FROM s.ordered_date) = "
        "(SELECT MAX(EXTRACT(MONTH FROM x.ordered_date)) FROM sales_items AS x)",
        _REV + f"FROM sales_items AS s WHERE {M9} AND "
        "EXTRACT(YEAR FROM s.ordered_date) = "
        "(SELECT MAX(p.product_id) FROM products AS p)",
        # Month compared with the year subquery.
        _REV + "FROM sales_items AS s WHERE "
        f"EXTRACT(MONTH FROM s.ordered_date) = {_SUB}",
        # Alias mismatch: filters on one dated relation, witness on another.
        "SELECT MAX(o.ordered_date) AS last_day, SUM(s.sale_amount) AS r "
        "FROM sales_items AS s JOIN orders AS o ON s.order_ref = o.order_ref "
        f"WHERE {M9} AND EXTRACT(YEAR FROM s.ordered_date) = {_SUB}",
        # The pattern inside a CTE, not the top-level query.
        "WITH l AS (SELECT MAX(s.ordered_date) AS last_day, "
        "SUM(s.sale_amount) AS r FROM sales_items AS s "
        f"WHERE {M9} AND EXTRACT(YEAR FROM s.ordered_date) = {_SUB}) "
        "SELECT l.last_day, l.r FROM l",
        # Another dated read besides the year subquery.
        "SELECT MAX(s.ordered_date) AS last_day, SUM(s.sale_amount) AS r, "
        "(SELECT SUM(t.sale_amount) FROM sales_items AS t) AS total "
        f"FROM sales_items AS s WHERE {M9} AND "
        f"EXTRACT(YEAR FROM s.ordered_date) = {_SUB}",
    ],
)
def test_unsupported_latest_shapes_stay_unknown(sql: str) -> None:
    compiled = compile_sql(sql)
    assert compiled.latest_month is None
    assert compiled.date_window is None
