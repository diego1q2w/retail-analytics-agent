# ruff: noqa: S608
"""Trusted period of the latest-month query shape, resolved from its result.

The compiler recognizes the shape and names the output columns that carry the
filtered rows' own dates; the year is read from those released dates after
execution (no extra query). Each case runs the compiled statement on the
DuckDB oracle and releases it through the real result privacy boundary.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import replace
from datetime import date, datetime

import duckdb
import pytest

from retail_analytics.application.contracts.query_compiler import (
    CompiledQuery,
    ScalarValue,
)
from retail_analytics.application.contracts.result_privacy import QueryRows
from retail_analytics.application.contracts.sql_dialect import LATEST_MONTH_EXAMPLE
from retail_analytics.application.evidence import latest_month_window, query_basis
from retail_analytics.application.result_privacy import (
    ReleasedResult,
    ResultPrivacyBoundary,
)
from retail_analytics.domain.metrics import default_catalog
from retail_analytics.domain.periods import DateWindow
from retail_analytics.domain.preferences import EffectivePreferences
from tests.unit.sql_compiler.support import (
    ALICE,
    BOB,
    RAW,
    compile_sql,
    database,
    execute,
    view,
)

NO_PREFERENCES = EffectivePreferences(())


@pytest.fixture
def db() -> Iterator[duckdb.DuckDBPyConnection]:
    connection = database()
    yield connection
    connection.close()


def _add_sale(
    db: duckdb.DuckDBPyConnection, order: int, item: int, product: int, at: str
) -> None:
    db.execute(f"INSERT INTO {RAW}orders VALUES ({order},10,'{at}',1,'Complete')")
    db.execute(
        f"INSERT INTO {RAW}order_items VALUES "
        f"({item},{order},10,{product},'Complete',5)"
    )


def _released(
    db: duckdb.DuckDBPyConnection,
    values: Mapping[str, ScalarValue],
    sql: str = LATEST_MONTH_EXAMPLE,
    who: object = ALICE,
) -> tuple[CompiledQuery, ReleasedResult]:
    compiled = compile_sql(sql, who, values)  # type: ignore[arg-type]
    rows = execute(db, compiled)
    columns = tuple(o.name for o in compiled.outputs)
    released = ResultPrivacyBoundary().release(
        compiled, QueryRows(columns, rows), catalog=view()
    )
    return compiled, released


def _period(compiled: CompiledQuery, released: ReleasedResult) -> DateWindow | None:
    return query_basis(
        compiled,
        metrics=default_catalog(),
        effective=NO_PREFERENCES,
        released=released,
    ).period


def test_latest_september_in_2026(db: duckdb.DuckDBPyConnection) -> None:
    _add_sale(db, 200, 2000, 1, "2025-09-03 10:00:00")
    compiled, released = _released(db, {"month": 9})
    assert _period(compiled, released) == DateWindow(
        date(2026, 9, 1), date(2026, 10, 1)
    )


def test_latest_september_in_an_older_year(db: duckdb.DuckDBPyConnection) -> None:
    # Remove every 2026 September sale: the latest September is 2023.
    db.execute(f"DELETE FROM {RAW}order_items WHERE order_id IN (100, 101, 102)")
    db.execute(f"DELETE FROM {RAW}orders WHERE order_id IN (100, 101, 102)")
    _add_sale(db, 200, 2000, 1, "2023-09-29 10:00:00")
    _add_sale(db, 201, 2001, 1, "2021-09-02 10:00:00")
    compiled, released = _released(db, {"month": 9})
    # The observed dates cover one day; the period is the whole calendar month.
    assert released.records()[0]["first_day"] == date(2023, 9, 29)
    assert _period(compiled, released) == DateWindow(
        date(2023, 9, 1), date(2023, 10, 1)
    )


def test_latest_february_in_a_leap_year(db: duckdb.DuckDBPyConnection) -> None:
    _add_sale(db, 200, 2000, 1, "2024-02-29 10:00:00")
    _add_sale(db, 201, 2001, 1, "2023-02-10 10:00:00")
    compiled, released = _released(db, {"month": 2})
    period = _period(compiled, released)
    assert period == DateWindow(date(2024, 2, 1), date(2024, 3, 1))
    assert period is not None and period.days == 29


def test_latest_year_is_per_product_scope(db: duckdb.DuckDBPyConnection) -> None:
    # Only Alice's product 1 has a 2027 September; Bob's latest stays 2026.
    _add_sale(db, 200, 2000, 1, "2027-09-01 00:00:00")
    alice = _period(*_released(db, {"month": 9}, who=ALICE))
    bob = _period(*_released(db, {"month": 9}, who=BOB))
    assert alice == DateWindow(date(2027, 9, 1), date(2027, 10, 1))
    assert bob == DateWindow(date(2026, 9, 1), date(2026, 10, 1))


def test_no_matching_rows_leaves_the_period_unknown(
    db: duckdb.DuckDBPyConnection,
) -> None:
    compiled, released = _released(db, {"month": 11})
    assert released.records() == [
        {"first_day": None, "last_day": None, "revenue": None}
    ]
    assert _period(compiled, released) is None


def test_grouped_rows_share_the_resolved_month(db: duckdb.DuckDBPyConnection) -> None:
    sql = (
        "SELECT s.ordered_date AS day, SUM(s.sale_amount) AS revenue "
        "FROM sales_items AS s WHERE EXTRACT(MONTH FROM s.ordered_date) = @month "
        "AND EXTRACT(YEAR FROM s.ordered_date) = (SELECT MAX(EXTRACT(YEAR FROM "
        "x.ordered_date)) FROM sales_items AS x) GROUP BY s.ordered_date"
    )
    compiled, released = _released(db, {"month": 9}, sql=sql, who=BOB)
    assert len(released.rows) == 2
    assert _period(compiled, released) == DateWindow(
        date(2026, 9, 1), date(2026, 10, 1)
    )
    # The latest year overall has no row in that month: honest empty, unknown.
    _add_sale(db, 200, 2000, 2, "2027-01-05 10:00:00")
    compiled, released = _released(db, {"month": 9}, sql=sql, who=BOB)
    assert released.rows == ()
    assert _period(compiled, released) is None


def test_without_a_released_result_the_period_is_unknown() -> None:
    compiled = compile_sql(LATEST_MONTH_EXAMPLE, values={"month": 9})
    basis = query_basis(compiled, metrics=default_catalog(), effective=NO_PREFERENCES)
    assert basis.period is None


def _with_rows(
    released: ReleasedResult, rows: tuple[tuple[object, ...], ...], **changes: object
) -> ReleasedResult:
    return replace(released, rows=rows, **changes)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("rows", "changes"),
    [
        # Witnesses disagree with each other.
        (((date(2026, 9, 1), date(2025, 9, 30), 1.0),), {}),
        # A date outside the filtered month (impossible for a faithful result).
        (((date(2026, 10, 1), date(2026, 10, 1), 1.0),), {}),
        # Not a date.
        (((datetime(2026, 9, 1), datetime(2026, 9, 1), 1.0),), {}),
        ((("2026-09-01", "2026-09-01", 1.0),), {}),
        # Witness columns withheld by the privacy boundary.
        (
            ((date(2026, 9, 1), date(2026, 9, 2), 1.0),),
            {"masked_columns": ("first_day", "last_day")},
        ),
    ],
)
def test_inconsistent_or_withheld_witnesses_stay_unknown(
    db: duckdb.DuckDBPyConnection,
    rows: tuple[tuple[object, ...], ...],
    changes: dict[str, object],
) -> None:
    compiled, released = _released(db, {"month": 9})
    assert latest_month_window(compiled, released) is not None
    assert latest_month_window(compiled, _with_rows(released, rows, **changes)) is None


def test_a_witness_name_must_carry_only_date_lineage(
    db: duckdb.DuckDBPyConnection,
) -> None:
    compiled, released = _released(db, {"month": 9})
    renamed = tuple(
        replace(c, name="first_day") if c.name == "revenue" else replace(c, name="x")
        for c in released.columns
    )
    forged = replace(released, columns=renamed)
    assert latest_month_window(compiled, forged) is None


def test_other_queries_never_use_result_dates(db: duckdb.DuckDBPyConnection) -> None:
    # Observed MIN/MAX dates alone are not a calendar window.
    sql = (
        "SELECT MIN(s.ordered_date) AS first_day, MAX(s.ordered_date) AS last_day "
        "FROM sales_items AS s WHERE EXTRACT(MONTH FROM s.ordered_date) = 9"
    )
    compiled, released = _released(db, {}, sql=sql)
    assert compiled.latest_month is None
    assert _period(compiled, released) is None
