"""End to end: compile with real derivations, execute, release through the boundary.

Covers the accepted policy: useful customer and demographic analysis with
opaque references and age bands, no direct identifiers or exact ages, no
minimum group size, references scoped per executive and rechecked against the
current product scope on every lookup.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator

import pytest
from duckdb import DuckDBPyConnection as Connection
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from retail_analytics.adapters.sql_compiler import (
    ReferenceKeyring,
    ScopedSqlglotCompilers,
)
from retail_analytics.application.contracts.query_compiler import AnalysisQuery
from retail_analytics.application.contracts.result_privacy import Cell
from retail_analytics.application.query_compiler import QueryRejected
from retail_analytics.application.result_privacy import ColumnRole
from retail_analytics.domain.access import ProductScope
from retail_analytics.domain.operations import ToolErrorCode
from retail_analytics.domain.privacy import is_age_band, is_reference
from tests.unit.privacy.support import (
    AGES,
    BOUNDARY,
    EXEC_A,
    EXEC_B,
    MASTER_KEY,
    SCOPE_A,
    SCOPE_B,
    assert_no_pii,
    cells,
    compile_for,
    customer_database,
    raw_rows,
    ref,
    released,
    set_age,
)
from tests.unit.sql_compiler.support import DATASET, scope, view

TOP_CUSTOMERS = (
    "SELECT s.customer_ref AS customer, c.state AS region, c.age_band AS band, "
    "SUM(s.sale_amount) AS completed_sales "
    "FROM sales_items s JOIN customers c ON s.customer_ref = c.customer_ref "
    "WHERE s.item_status = 'Complete' "
    "GROUP BY customer, region, band "
    "ORDER BY completed_sales DESC LIMIT 10"
)


@pytest.fixture
def db() -> Iterator[Connection]:
    connection = customer_database()
    yield connection
    connection.close()


# --- useful results without identifiers -----------------------------------------------


def test_top_customers_are_useful_without_identifiers(db: Connection) -> None:
    result = released(db, EXEC_A, TOP_CUSTOMERS)

    assert result.rows == (
        (ref(EXEC_A, "customer_ref", 30), "CA", "25-29", 45.0),
        (ref(EXEC_A, "customer_ref", 10), "CA", "25-29", 40.0),
        (ref(EXEC_A, "customer_ref", 40), "TX", "90+", 12.0),
        (ref(EXEC_A, "customer_ref", 50), "NY", None, 5.0),
    )
    roles = {c.name: c.role for c in result.columns}
    assert roles == {
        "customer": ColumnRole.REFERENCE,
        "region": ColumnRole.VALUE,
        "band": ColumnRole.AGE_BAND,
        "completed_sales": ColumnRole.VALUE,
    }
    assert result.truncation is None and result.masked_cells == 0
    assert_no_pii(result, raw_ids=(10, 30, 40, 50))


def test_individual_customer_detail_includes_demographics(db: Connection) -> None:
    result = released(
        db,
        EXEC_A,
        "SELECT customer_ref, country, state, age_band FROM customers "
        "ORDER BY state, customer_ref",
    )
    assert sorted(result.rows, key=lambda r: str(r[0])) == sorted(
        [
            (ref(EXEC_A, "customer_ref", 10), "US", "CA", "25-29"),
            (ref(EXEC_A, "customer_ref", 30), "US", "CA", "25-29"),
            (ref(EXEC_A, "customer_ref", 50), "US", "NY", None),
            (ref(EXEC_A, "customer_ref", 40), "US", "TX", "90+"),
        ],
        key=lambda r: str(r[0]),
    )
    assert_no_pii(result, raw_ids=(10, 30, 40, 50))


def test_tiny_demographic_groups_are_released_without_suppression(
    db: Connection,
) -> None:
    # Policy positive control: no minimum group size, single-customer groups
    # are returned as they are.
    result = released(
        db,
        EXEC_A,
        "SELECT state, age_band, COUNT(DISTINCT customer_ref) AS n_customers "
        "FROM customers GROUP BY state, age_band ORDER BY state",
    )
    assert result.rows == (("CA", "25-29", 2), ("NY", None, 1), ("TX", "90+", 1))
    assert_no_pii(result)


def test_analysis_can_choose_coarser_bands(db: Connection) -> None:
    result = released(
        db,
        EXEC_A,
        "SELECT CASE WHEN c.age_band IN ('20-24', '25-29', '30-34') THEN '20-34' "
        "WHEN c.age_band IS NULL THEN 'unknown' ELSE '35+' END AS age_group, "
        "SUM(s.sale_amount) AS sales FROM sales_items s "
        "JOIN customers c ON s.customer_ref = c.customer_ref "
        "WHERE s.item_status = 'Complete' GROUP BY 1 ORDER BY 1",
    )
    assert result.rows == (("20-34", 85.0), ("35+", 12.0), ("unknown", 5.0))
    # A derived band is an ordinary value, not a passthrough age band.
    assert result.columns[0].role is ColumnRole.VALUE


def test_order_and_item_references_are_opaque(db: Connection) -> None:
    result = released(
        db,
        EXEC_A,
        "SELECT item_ref, order_ref, customer_ref FROM sales_items "
        "WHERE ordered_date = DATE '2026-09-15'",
    )
    assert result.rows == (
        (
            ref(EXEC_A, "item_ref", 1005),
            ref(EXEC_A, "order_ref", 104),
            ref(EXEC_A, "customer_ref", 30),
        ),
    )
    assert_no_pii(result, raw_ids=(1005, 104, 30))


def test_references_join_consistently_across_relations(db: Connection) -> None:
    result = released(
        db,
        EXEC_A,
        "SELECT o.order_ref, c.age_band, o.visible_item_count FROM orders o "
        "JOIN customers c ON o.customer_ref = c.customer_ref "
        "WHERE o.ordered_date = DATE '2026-09-10'",
    )
    assert result.rows == ((ref(EXEC_A, "order_ref", 100), "25-29", 1),)


# --- drill-down by reference ----------------------------------------------------------


def _orders_of(
    db: Connection, executive: str, customer: str, who: ProductScope
) -> tuple[tuple[Cell, ...], ...]:
    return released(
        db,
        executive,
        "SELECT s.order_ref, s.product_id, s.sale_amount FROM sales_items s "
        "WHERE s.customer_ref = @customer ORDER BY s.sale_amount",
        who,
        {"customer": customer},
    ).rows


def test_drill_down_by_reference_returns_permitted_history(db: Connection) -> None:
    customer = ref(EXEC_A, "customer_ref", 30)
    assert _orders_of(db, EXEC_A, customer, SCOPE_A) == (
        (ref(EXEC_A, "order_ref", 104), 1, 45.0),
    )


def test_reference_lookup_rechecks_current_product_scope(db: Connection) -> None:
    customer = ref(EXEC_A, "customer_ref", 10)
    assert len(_orders_of(db, EXEC_A, customer, SCOPE_A)) == 3
    narrowed = scope(3, version=SCOPE_A.entitlement_version + 1)
    # Same executive, same reference: product 1 history is no longer visible.
    assert _orders_of(db, EXEC_A, customer, narrowed) == ()


def test_wrong_executive_reference_matches_nothing(db: Connection) -> None:
    # Customer 30 bought from both executives' products.
    from_a = ref(EXEC_A, "customer_ref", 30)
    from_b = ref(EXEC_B, "customer_ref", 30)
    assert from_a != from_b
    assert _orders_of(db, EXEC_B, from_a, SCOPE_B) == ()
    assert _orders_of(db, EXEC_B, from_b, SCOPE_B) == (
        (ref(EXEC_B, "order_ref", 107), 2, 60.0),
    )


def test_references_cannot_be_joined_across_executives(db: Connection) -> None:
    a_refs = {
        row[0]
        for row in released(db, EXEC_A, "SELECT customer_ref FROM customers").rows
    }
    b_refs = {
        row[0]
        for row in released(
            db, EXEC_B, "SELECT customer_ref FROM customers", SCOPE_B
        ).rows
    }
    # Customers 10 and 30 are in both scopes, yet no reference coincides.
    assert len(a_refs) == 4 and len(b_refs) == 3
    assert not a_refs & b_refs


def test_references_are_stable_for_one_executive_across_compilers(
    db: Connection,
) -> None:
    # A fresh keyring from the same master key (another process, a later
    # session) yields the same references for the same executive.
    again = ScopedSqlglotCompilers(DATASET, ReferenceKeyring(MASTER_KEY))
    compiled = again.for_executive(EXEC_A).compile(
        AnalysisQuery("SELECT customer_ref FROM customers"),
        catalog=view(),
        scope=SCOPE_A,
    )
    rows = BOUNDARY.release(compiled, raw_rows(db, compiled), catalog=view()).rows
    assert {r[0] for r in rows} == {
        ref(EXEC_A, "customer_ref", raw) for raw in (10, 30, 40, 50)
    }


def test_reference_kinds_are_domain_separated() -> None:
    assert ref(EXEC_A, "customer_ref", 104) != ref(EXEC_A, "order_ref", 104)
    assert ref(EXEC_A, "customer_ref", 104).startswith("cus_")
    assert ref(EXEC_A, "order_ref", 104).startswith("ord_")


def test_references_are_not_unkeyed_hashes() -> None:
    reference = ref(EXEC_A, "customer_ref", 10)
    digest = reference.removeprefix("cus_")
    for message in (b"10", b"customer_ref:10"):
        for algorithm in ("sha256", "md5", "sha1", "sha512"):
            assert not hashlib.new(algorithm, message).hexdigest().startswith(digest)
    assert "10" not in reference.split("_", 1)[0]


def test_rotating_the_master_key_retires_references() -> None:
    rotated = ReferenceKeyring(b"another-test-only-master-key-abcdefghijk")
    assert rotated.for_executive(EXEC_A).reference("customer_ref", 10) != ref(
        EXEC_A, "customer_ref", 10
    )


def test_user_supplied_raw_id_does_not_find_a_customer(db: Connection) -> None:
    for guess in ("10", "cus_10", "customer_ref:10"):
        assert _orders_of(db, EXEC_A, guess, SCOPE_A) == ()


# --- exact age and raw keys stay out of reach -----------------------------------------

FORBIDDEN = [
    "SELECT age FROM customers",
    "SELECT customer_ref FROM customers WHERE age BETWEEN 27 AND 27",
    "SELECT CAST(age AS STRING) AS a FROM customers",
    "SELECT SAFE_DIVIDE(age, 1) AS a FROM customers",
    "SELECT age_band FROM customers WHERE age = 27",
    "SELECT age_band, MIN(age) AS youngest FROM customers GROUP BY age_band",
    "WITH x AS (SELECT age AS a FROM customers) SELECT a FROM x",
    "WITH customers AS (SELECT age FROM customers) SELECT age FROM customers",
    "SELECT (SELECT MAX(age) FROM customers) AS oldest FROM products",
    "SELECT customer_ref FROM customers ORDER BY age LIMIT 1",
    "SELECT COUNT(*) AS n FROM customers GROUP BY age",
    "SELECT customer_ref FROM customers WHERE id = 10",
    "SELECT customer_ref FROM sales_items WHERE user_id = 10",
    "SELECT order_ref FROM sales_items WHERE order_id = 100",
    "SELECT s.customer_ref FROM sales_items s WHERE s.id = 1000",
    "SELECT customer_ref, email FROM customers",
    "SELECT customer_ref FROM customers WHERE first_name = 'Alice'",
    "SELECT customer_ref FROM customers WHERE city = 'Springfield'",
    "SELECT customer_ref, postal_code FROM customers",
    "SELECT customer_ref FROM customers WHERE latitude > 0",
]


@pytest.mark.parametrize("sql", FORBIDDEN)
def test_exact_age_identifiers_and_raw_keys_are_rejected(sql: str) -> None:
    with pytest.raises(QueryRejected) as caught:
        compile_for(EXEC_A, sql, SCOPE_A)
    assert caught.value.code is ToolErrorCode.FIELD_UNAVAILABLE


# Queries that try to resolve age finer than the grid in every way the grammar
# allows (filters, ordering, extremes, string comparison, merged bands).
AGE_PROBES = [
    "SELECT age_band, COUNT(*) AS n FROM customers GROUP BY age_band ORDER BY age_band",
    "SELECT MIN(age_band) AS lo, MAX(age_band) AS hi FROM customers",
    "SELECT customer_ref, age_band FROM customers ORDER BY age_band, customer_ref",
    "SELECT COUNT(*) AS n FROM customers WHERE age_band LIKE '2%'",
    "SELECT customer_ref FROM customers WHERE age_band > '26' ORDER BY customer_ref",
    "SELECT customer_ref FROM customers WHERE age_band BETWEEN '27' AND '28'",
    "SELECT LOWER(age_band) AS b, COUNT(*) AS n FROM customers GROUP BY 1 ORDER BY 1",
    "SELECT c.state, CASE WHEN c.age_band IN ('25-29', '30-34') THEN 'young' "
    "ELSE 'other' END AS seg, SUM(s.sale_amount) AS total FROM sales_items s "
    "JOIN customers c ON s.customer_ref = c.customer_ref GROUP BY 1, 2 ORDER BY 1, 2",
    "SELECT COUNT(DISTINCT customer_ref) AS n FROM customers "
    "WHERE age_band = '25-29' OR age_band = '90+'",
]


def _cell_age(age: int | None, offset: int) -> int | None:
    """Another age in the same grid cell as ``age``."""
    if age is None:
        return None
    if age >= 90:
        return 90 + offset * 7
    return age // 5 * 5 + offset % 5


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(offsets=st.lists(st.integers(0, 4), min_size=5, max_size=5))
def test_no_query_resolves_age_below_the_grid(offsets: list[int]) -> None:
    db = customer_database()
    try:
        baseline = {
            (sql, who): released(
                db,
                EXEC_A if who == "a" else EXEC_B,
                sql,
                SCOPE_A if who == "a" else SCOPE_B,
            ).rows
            for sql in AGE_PROBES
            for who in ("a", "b")
        }
        for user, offset in zip(AGES, offsets, strict=True):
            set_age(db, user, _cell_age(AGES[user], offset))
        for (sql, who), rows in baseline.items():
            again = released(
                db,
                EXEC_A if who == "a" else EXEC_B,
                sql,
                SCOPE_A if who == "a" else SCOPE_B,
            ).rows
            assert again == rows, sql
    finally:
        db.close()


def test_age_band_cells_are_well_formed(db: Connection) -> None:
    for age in (0, 4, 5, 17, 18, 64, 89, 90, 130):
        set_age(db, 10, age)
        rows = released(
            db,
            EXEC_A,
            "SELECT customer_ref, age_band FROM customers WHERE customer_ref = @c",
            values={"c": ref(EXEC_A, "customer_ref", 10)},
        ).rows
        (band,) = [r[1] for r in rows]
        assert is_age_band(band), (age, band)


# --- configuration and secrets --------------------------------------------------------


def test_references_fail_closed_without_a_key() -> None:
    compilers = ScopedSqlglotCompilers(DATASET, None)
    with pytest.raises(QueryRejected) as caught:
        compile_for(
            EXEC_A, "SELECT customer_ref FROM customers", SCOPE_A, compilers=compilers
        )
    assert caught.value.code is ToolErrorCode.FIELD_UNAVAILABLE
    assert caught.value.reason == "derivation_unavailable"
    # Age bands need no key and stay available.
    compiled = compile_for(
        EXEC_A,
        "SELECT age_band, COUNT(*) AS n FROM customers GROUP BY age_band",
        SCOPE_A,
        compilers=compilers,
    )
    assert not any(p.secret for p in compiled.parameters)


def test_key_material_stays_out_of_model_facing_values(db: Connection) -> None:
    compiled = compile_for(EXEC_A, TOP_CUSTOMERS, SCOPE_A)
    secrets = [p for p in compiled.parameters if p.secret]
    assert {p.name for p in secrets} == {"_policy_ref_inner", "_policy_ref_outer"}
    assert all(p.trusted for p in secrets)
    assert not any(p.secret for p in compiled.analysis_parameters)
    for parameter in secrets:
        value = str(parameter.value)
        assert value not in compiled.sql and value not in compiled.logical_sql
        assert value not in repr(compiled) and value not in repr(parameter)
    result = BOUNDARY.release(compiled, raw_rows(db, compiled), catalog=view())
    payload = repr(result.records())
    assert all(str(p.value) not in payload for p in secrets)
    assert MASTER_KEY.decode() not in payload


def test_released_reference_columns_hold_only_references(db: Connection) -> None:
    result = released(db, EXEC_A, TOP_CUSTOMERS)
    assert all(is_reference(c, "customer_ref") for c in cells(result)[::4])
