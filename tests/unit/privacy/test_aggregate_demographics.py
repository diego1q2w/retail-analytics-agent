"""Customer demographics are aggregate-only (T09-F1).

Real keyed derivations over the DuckDB oracle: group-level demographic
statistics are released with correct, scope-limited values; every way of
showing a demographic for one customer, order or item is refused by the
compiler before anything runs, and the result boundary independently refuses
demographic results the compiler did not verify as group-level.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace

import pytest
from duckdb import DuckDBPyConnection as Connection

from retail_analytics.adapters.sql_compiler import SqlglotGrainAudit
from retail_analytics.application.contracts.query_compiler import DemographicUse
from retail_analytics.application.contracts.result_privacy import QueryRows
from retail_analytics.application.query_compiler import QueryRejected
from retail_analytics.application.result_privacy import ResultWithheld
from retail_analytics.domain.operations import ToolErrorCode
from tests.unit.privacy.support import (
    BOUNDARY,
    EXEC_A,
    EXEC_B,
    SCOPE_A,
    SCOPE_B,
    assert_no_pii,
    compile_for,
    customer_database,
    raw_rows,
    ref,
    released,
)
from tests.unit.sql_compiler.support import view

REF = "cus_" + "0" * 24


@pytest.fixture
def db() -> Iterator[Connection]:
    connection = customer_database()
    yield connection
    connection.close()


def _sorted(rows: tuple[tuple[object, ...], ...]) -> list[tuple[object, ...]]:
    return sorted(rows, key=lambda r: tuple(str(c) for c in r))


# --- group-level statistics stay available ---------------------------------------

SALES_BY_STATE = (
    "SELECT c.state, COUNT(DISTINCT s.customer_ref) AS customers, "
    "SUM(s.sale_amount) AS sales "
    "FROM sales_items s JOIN customers c ON s.customer_ref = c.customer_ref "
    "WHERE s.item_status = 'Complete' GROUP BY c.state ORDER BY c.state"
)


def test_state_breakdown_counts_only_permitted_purchases(db: Connection) -> None:
    # Customer 10's order 100 and customer 30 also bought product 2, which A
    # may not see: CA is 30 + 10 + 45, never including the 70 or 60.
    result = released(db, EXEC_A, SALES_BY_STATE)
    assert _sorted(result.rows) == [("CA", 2, 85.0), ("NY", 1, 5.0), ("TX", 1, 12.0)]
    assert_no_pii(result, raw_ids=(10, 20, 30, 40, 50))


def test_other_executive_sees_only_their_products(db: Connection) -> None:
    result = released(db, EXEC_B, SALES_BY_STATE, SCOPE_B)
    assert _sorted(result.rows) == [("CA", 2, 130.0), ("NY", 1, 90.0)]


def test_age_band_breakdown_is_released(db: Connection) -> None:
    result = released(
        db,
        EXEC_A,
        "SELECT c.age_band, COUNT(DISTINCT s.customer_ref) AS customers, "
        "SUM(s.sale_amount) AS sales FROM sales_items s "
        "JOIN customers c ON s.customer_ref = c.customer_ref "
        "WHERE s.item_status = 'Complete' GROUP BY c.age_band",
    )
    assert _sorted(result.rows) == [
        ("25-29", 2, 85.0),
        ("90+", 1, 12.0),
        (None, 1, 5.0),
    ]


def test_per_customer_steps_may_feed_a_population_breakdown(db: Connection) -> None:
    # Average spend per customer by age band: the per-customer CTE never
    # reaches the result, which is grouped by band only.
    result = released(
        db,
        EXEC_A,
        "WITH per AS (SELECT s.customer_ref AS k, MAX(c.age_band) AS band, "
        "SUM(s.sale_amount) AS spend FROM sales_items s "
        "JOIN customers c ON s.customer_ref = c.customer_ref "
        "WHERE s.item_status = 'Complete' GROUP BY s.customer_ref) "
        "SELECT band, COUNT(*) AS customers, AVG(spend) AS avg_spend "
        "FROM per GROUP BY band",
    )
    assert _sorted(result.rows) == [
        ("25-29", 2, 42.5),
        ("90+", 1, 12.0),
        (None, 1, 5.0),
    ]


def test_naturally_small_groups_are_released(db: Connection) -> None:
    # No minimum group size (client: aggregate-only, no numeric threshold):
    # TX/90+ is a group of one customer and is still a group statistic.
    result = released(
        db,
        EXEC_A,
        "SELECT c.state, c.age_band, COUNT(DISTINCT c.customer_ref) AS n "
        "FROM customers c GROUP BY 1, 2 HAVING COUNT(DISTINCT c.customer_ref) = 1",
    )
    assert ("TX", "90+", 1) in result.rows
    assert all(n == 1 for _, _, n in result.rows)


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        (
            "SELECT c.state, COUNT(*) AS n FROM customers c WHERE c.customer_ref IN "
            "(SELECT s.customer_ref FROM sales_items s "
            "WHERE s.item_status = 'Shipped') GROUP BY c.state",
            [("CA", 1)],
        ),
        ("SELECT DISTINCT c.state FROM customers c", [("CA",), ("NY",), ("TX",)]),
        (
            "SELECT COUNT(*) AS n FROM customers c WHERE c.age_band = '25-29'",
            [(2,)],
        ),
        (
            "SELECT c.state, COUNT(*) AS n FROM customers c GROUP BY c.state "
            "ORDER BY n DESC LIMIT 1",
            [("CA", 2)],
        ),
    ],
)
def test_population_filters_and_rankings_of_groups_are_allowed(
    db: Connection, sql: str, expected: list[tuple[object, ...]]
) -> None:
    result = released(db, EXEC_A, sql)
    assert _sorted(result.rows) == expected
    assert compile_for(EXEC_A, sql, SCOPE_A).demographic_use is DemographicUse.AGGREGATE


def test_customer_analysis_without_demographics_is_unchanged(db: Connection) -> None:
    top = released(
        db,
        EXEC_A,
        "SELECT s.customer_ref, SUM(s.sale_amount) AS spend FROM sales_items s "
        "WHERE s.item_status = 'Complete' GROUP BY s.customer_ref "
        "ORDER BY spend DESC LIMIT 2",
    )
    assert top.rows == (
        (ref(EXEC_A, "customer_ref", 30), 45.0),
        (ref(EXEC_A, "customer_ref", 10), 40.0),
    )
    history = released(
        db,
        EXEC_A,
        "SELECT s.order_ref, s.sale_amount FROM sales_items s "
        "WHERE s.customer_ref = @c",
        values={"c": ref(EXEC_A, "customer_ref", 30)},
    )
    assert [amount for _, amount in history.rows] == [45.0]
    compiled = compile_for(EXEC_A, "SELECT customer_ref FROM customers", SCOPE_A)
    assert compiled.demographic_use is DemographicUse.NONE


# --- individual demographics are refused before execution -------------------------

PROFILES = [
    # direct profiles
    "SELECT customer_ref, country, state, age_band FROM customers",
    "SELECT country, state, age_band FROM customers",
    "SELECT o.order_ref, c.state FROM orders o "
    "JOIN customers c ON o.customer_ref = c.customer_ref",
    "SELECT s.item_ref, c.age_band FROM sales_items s "
    "JOIN customers c ON s.customer_ref = c.customer_ref",
    "SELECT DISTINCT c.customer_ref, c.state FROM customers c",
    "SELECT c.state, c.age_band FROM customers c ORDER BY c.state LIMIT 1",
    # demographics only in a predicate still profile the listed rows
    "SELECT s.item_ref, s.sale_amount FROM sales_items s "
    "JOIN customers c ON s.customer_ref = c.customer_ref WHERE c.state = 'CA'",
    "SELECT s.customer_ref, SUM(s.sale_amount) AS t FROM sales_items s "
    "JOIN customers c ON s.customer_ref = c.customer_ref "
    "WHERE c.age_band = '25-29' GROUP BY s.customer_ref",
    # identity-grained aggregations
    "SELECT s.customer_ref, MAX(c.age_band) AS band FROM sales_items s "
    "JOIN customers c ON s.customer_ref = c.customer_ref GROUP BY s.customer_ref",
    "SELECT LOWER(c.customer_ref) AS k, c.state FROM customers c GROUP BY 1, 2",
    "SELECT c.state, MIN(c.customer_ref) AS someone FROM customers c GROUP BY c.state",
    "SELECT s.order_ref, MAX(c.state) AS st, SUM(s.sale_amount) AS t "
    "FROM sales_items s JOIN customers c ON s.customer_ref = c.customer_ref "
    "GROUP BY s.order_ref",
    # derived aliases and CTEs that keep individual grain
    "WITH p AS (SELECT c.customer_ref AS k, c.state AS st FROM customers c) "
    "SELECT k AS who, st, COUNT(*) AS n FROM p GROUP BY k, st",
    "WITH per AS (SELECT s.customer_ref AS k, MAX(c.state) AS st "
    "FROM sales_items s JOIN customers c ON s.customer_ref = c.customer_ref "
    "GROUP BY s.customer_ref) SELECT st FROM per",
    "SELECT x.st FROM (SELECT c.state AS st FROM customers c) AS x",
    # identity-targeted, wrapped in aggregates
    "SELECT c.state, COUNT(*) AS n FROM customers c WHERE c.customer_ref = @r "
    "GROUP BY c.state",
    "SELECT MAX(c.age_band) AS band FROM customers c WHERE c.customer_ref = @r",
    f"SELECT c.age_band, COUNT(*) AS n FROM customers c "
    f"WHERE c.customer_ref IN ('{REF}') GROUP BY c.age_band",
    "SELECT c.state, COUNT(*) AS n FROM customers c "
    "WHERE c.customer_ref > 'cus_8' GROUP BY 1",
    "SELECT c.state, COUNTIF(c.customer_ref = @r) AS hit FROM customers c "
    "GROUP BY c.state",
    "SELECT CASE WHEN c.customer_ref = @r THEN c.age_band ELSE 'other' END AS b, "
    "COUNT(*) AS n FROM customers c GROUP BY 1",
    "SELECT c.state, COUNT(*) AS n FROM customers c WHERE c.customer_ref IN "
    "(SELECT s.customer_ref FROM sales_items s WHERE s.order_ref = @r) "
    "GROUP BY c.state",
    "SELECT c.state, COUNT(*) AS n FROM sales_items s "
    "JOIN customers c ON s.customer_ref = c.customer_ref "
    "WHERE s.item_ref = @r GROUP BY c.state",
    "SELECT (SELECT c.age_band FROM customers c WHERE c.customer_ref = @r) AS b",
    # rank-selected individuals (top-N customers) then their demographics
    "WITH top AS (SELECT s.customer_ref AS k, SUM(s.sale_amount) AS t "
    "FROM sales_items s GROUP BY s.customer_ref ORDER BY t DESC LIMIT 1) "
    "SELECT c.age_band, COUNT(*) AS n FROM customers c "
    "WHERE c.customer_ref IN (SELECT k FROM top) GROUP BY c.age_band",
]


def _values(sql: str) -> dict[str, object]:
    return {"r": REF} if "@r" in sql else {}


@pytest.mark.parametrize("sql", PROFILES)
def test_individual_demographics_are_rejected(sql: str) -> None:
    with pytest.raises(QueryRejected) as caught:
        compile_for(EXEC_A, sql, SCOPE_A, _values(sql))  # type: ignore[arg-type]
    assert caught.value.reason == "individual_demographics"
    assert caught.value.code is ToolErrorCode.UNSUPPORTED_SQL
    assert caught.value.correctable
    assert "group-level" in caught.value.message


@pytest.mark.parametrize("sql", PROFILES)
def test_legacy_audit_agrees_with_the_compiler(sql: str) -> None:
    # The stored logical form of an old query is judged the same way.
    audit = SqlglotGrainAudit()
    assert not audit.aggregate_only(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT ARRAY_AGG(c.customer_ref) AS refs, c.state FROM customers c "
        "GROUP BY c.state",
        "SELECT STRING_AGG(c.customer_ref, ',') AS refs, c.state FROM customers c "
        "GROUP BY c.state",
        "SELECT CONCAT(c.customer_ref, c.state) AS x FROM customers c",
        "SELECT c.state, ROW_NUMBER() OVER (ORDER BY c.customer_ref) AS n "
        "FROM customers c",
    ],
)
def test_identity_arrays_and_concatenations_stay_outside_the_subset(sql: str) -> None:
    with pytest.raises(QueryRejected) as caught:
        compile_for(EXEC_A, sql, SCOPE_A)
    assert caught.value.code is ToolErrorCode.UNSUPPORTED_SQL


def test_legacy_audit_accepts_group_level_queries() -> None:
    audit = SqlglotGrainAudit()
    compiled = compile_for(EXEC_A, SALES_BY_STATE, SCOPE_A)
    assert audit.aggregate_only(compiled.logical_sql)
    assert not audit.aggregate_only("not sql at all (")
    assert not audit.aggregate_only("SELECT 1; SELECT 2")


# --- the result boundary checks independently --------------------------------------


def test_boundary_withholds_demographics_without_verified_grain(
    db: Connection,
) -> None:
    compiled = compile_for(EXEC_A, SALES_BY_STATE, SCOPE_A)
    unverified = replace(compiled, demographic_use=DemographicUse.NONE)
    with pytest.raises(ResultWithheld) as caught:
        BOUNDARY.release(unverified, raw_rows(db, compiled), catalog=view())
    assert caught.value.reason == "individual_demographics"


def test_boundary_withholds_reference_values_in_demographic_results() -> None:
    compiled = compile_for(EXEC_A, SALES_BY_STATE, SCOPE_A)
    leaked = QueryRows(
        tuple(o.name for o in compiled.outputs),
        [(ref(EXEC_A, "customer_ref", 10), 1, 5.0)],
    )
    with pytest.raises(ResultWithheld) as caught:
        BOUNDARY.release(compiled, leaked, catalog=view())
    assert caught.value.reason == "individual_demographics"


def test_boundary_judges_demographics_by_the_current_catalog(db: Connection) -> None:
    # A view that no longer marks the field (e.g. a tampered catalog) does not
    # matter for a query verified as aggregate; a query verified as NONE that
    # reads a field the current view marks demographic is withheld.
    compiled = compile_for(EXEC_A, "SELECT COUNT(*) AS n FROM customers", SCOPE_A)
    assert compiled.demographic_use is DemographicUse.NONE
    result = BOUNDARY.release(compiled, raw_rows(db, compiled), catalog=view())
    assert result.rows == ((4,),)
