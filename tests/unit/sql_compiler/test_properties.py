"""Property tests: hidden data cannot influence results; arbitrary input fails safe."""

from __future__ import annotations

import sqlglot
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from sqlglot import exp

from retail_analytics.application.contracts.query_compiler import CompiledQuery
from retail_analytics.application.query_compiler import QueryRejected
from tests.unit.sql_compiler.support import (
    ALICE,
    DATASET,
    RAW,
    compile_sql,
    database,
    execute,
    view,
)
from tests.unit.sql_compiler.test_contract import assert_scoped

# Alice's authorized questions, including demographics and declared joins.
OBSERVED = [
    "SELECT SUM(sale_amount) AS t FROM sales_items",
    "SELECT s.sale_amount,o.visible_item_count FROM sales_items s JOIN orders o "
    "ON s.order_ref=o.order_ref WHERE s.ordered_date=DATE '2026-09-10'",
    "SELECT product_id,product_name,catalog_price FROM products ORDER BY product_id",
    "SELECT customer_ref, state, country, age_band FROM customers "
    "ORDER BY customer_ref",
    "WITH sales_items AS (SELECT sale_amount FROM sales_items) "
    "SELECT SUM(sale_amount) AS t FROM sales_items",
    "SELECT order_ref, visible_item_count FROM orders ORDER BY order_ref",
    "SELECT c.state, c.age_band, COUNT(*) AS n, SUM(s.sale_amount) AS t "
    "FROM sales_items s JOIN customers c ON s.customer_ref = c.customer_ref "
    "GROUP BY c.state, c.age_band",
    "SELECT COUNT(DISTINCT customer_ref) AS buyers, COUNT(DISTINCT order_ref) AS o "
    "FROM sales_items",
]


@given(
    hidden_amount=st.integers(min_value=0, max_value=1_000_000),
    extra_hidden=st.integers(min_value=0, max_value=8),
    hidden_age=st.integers(min_value=0, max_value=120),
    hidden_state=st.sampled_from(["TX", "CA", "WA", "NY"]),
    own_email=st.emails(),
)
@settings(max_examples=60, deadline=None)
def test_hidden_data_does_not_change_authorized_results(
    hidden_amount: int,
    extra_hidden: int,
    hidden_age: int,
    hidden_state: str,
    own_email: str,
) -> None:
    db = database()
    try:
        compiled = [compile_sql(q, ALICE) for q in OBSERVED]
        before = [execute(db, c) for c in compiled]
        # Product 2 belongs to Bob: its items, product row and buyer change.
        db.execute(
            f"UPDATE {RAW}order_items SET sale_price=? WHERE product_id=2",
            [hidden_amount],
        )
        db.execute(
            f"UPDATE {RAW}products SET name='New hidden name', retail_price=? "
            "WHERE id=2",
            [hidden_amount],
        )
        # User 20 bought only hidden products.
        db.execute(
            f"UPDATE {RAW}users SET email='different@example.invalid', state=?, age=? "
            "WHERE id=20",
            [hidden_state, hidden_age],
        )
        # Forbidden fields of a visible customer are never part of any result.
        db.execute(
            f"UPDATE {RAW}users SET email=?, first_name='Changed', "
            "street_address='Elsewhere', city='Other' WHERE id=10",
            [own_email],
        )
        for n in range(extra_hidden):
            db.execute(
                f"INSERT INTO {RAW}order_items VALUES (?,100,10,2,'Complete',?)",
                [2000 + n, hidden_amount],
            )
        db.execute(
            f"UPDATE {RAW}orders SET num_of_item=2+?, status='Returned' "
            "WHERE order_id=100",
            [extra_hidden],
        )
        assert [execute(db, c) for c in compiled] == before
    finally:
        db.close()


# --- generated analytical queries -------------------------------------------------

_DIMENSIONS = [
    "s.product_id",
    "s.item_status",
    "s.ordered_date",
    "p.category",
    "p.brand",
    "p.department",
    "c.state",
    "c.country",
    "c.age_band",
]
_MEASURES = [
    "SUM(s.sale_amount)",
    "COUNT(*)",
    "COUNT(DISTINCT s.order_ref)",
    "COUNT(DISTINCT s.customer_ref)",
    "AVG(s.sale_amount)",
    "MAX(p.catalog_price)",
    "SAFE_DIVIDE(SUM(s.sale_amount), COUNT(DISTINCT s.order_ref))",
]
_FILTERS = [
    "TRUE",
    "s.item_status = 'Complete'",
    "s.ordered_date >= DATE '2026-09-01'",
    "c.age_band IS NOT NULL",
    "p.category LIKE 'C%'",
    "s.sale_amount BETWEEN 5 AND 100",
]


@st.composite
def analytical_queries(draw: st.DrawFn) -> str:
    dimension = draw(st.sampled_from(_DIMENSIONS))
    measures = draw(st.lists(st.sampled_from(_MEASURES), min_size=1, max_size=3))
    where = draw(st.sampled_from(_FILTERS))
    joins = (
        "JOIN products p ON s.product_id = p.product_id "
        "LEFT JOIN customers c ON s.customer_ref = c.customer_ref"
    )
    items = ", ".join(f"{m} AS m{i}" for i, m in enumerate(measures))
    query = (
        f"SELECT {dimension} AS d, {items} FROM sales_items s {joins} "
        f"WHERE {where} GROUP BY d"
    )
    if draw(st.booleans()):
        return f"WITH q AS ({query}) SELECT d, m0 FROM q"
    return query


@given(query=analytical_queries(), hidden_amount=st.integers(0, 10_000))
@settings(max_examples=60, deadline=None)
def test_generated_queries_are_scoped_and_hidden_data_independent(
    query: str, hidden_amount: int
) -> None:
    compiled = compile_sql(query, ALICE)
    assert_scoped(compiled)
    db = database()
    try:
        before = sorted(execute(db, compiled), key=repr)
        db.execute(
            f"UPDATE {RAW}order_items SET sale_price=?, status='Complete' "
            "WHERE product_id=2",
            [hidden_amount],
        )
        db.execute(f"UPDATE {RAW}users SET state='ZZ', age=99 WHERE id=20")
        assert sorted(execute(db, compiled), key=repr) == before
    finally:
        db.close()


# --- arbitrary input -----------------------------------------------------------------

_TOKENS = [
    "SELECT",
    "FROM",
    "WHERE",
    "GROUP BY",
    "ORDER BY",
    "HAVING",
    "WITH",
    "AS",
    "JOIN",
    "ON",
    "LEFT",
    "AND",
    "OR",
    "NOT",
    "IN",
    "(",
    ")",
    ",",
    ";",
    "*",
    "=",
    "<",
    "+",
    "/",
    "--",
    "/*",
    "*/",
    "`",
    "'",
    '"',
    "@",
    "@@",
    ".",
    "1",
    "'x'",
    "NULL",
    "COUNT(*)",
    "SUM(",
    "products",
    "sales_items",
    "customers",
    "orders",
    "users",
    "order_items",
    "email",
    "age",
    "age_band",
    "customer_ref",
    "product_id",
    "s",
    "c",
    "x",
    "UNION ALL",
    "DELETE",
    "EXCEPT",
    "_policy_product_ids",
    "UNNEST",
    "`bigquery-public-data.thelook_ecommerce.users`",
    "INFORMATION_SCHEMA",
]


def _assert_safe(compiled: CompiledQuery) -> None:
    assert_scoped(compiled)
    published = {
        (name, f.name)
        for name, relation in view().relations.items()
        for f in relation.fields
    }
    assert {(f.relation, f.field) for f in compiled.fields} <= published
    tree = sqlglot.parse_one(compiled.sql, read="bigquery")
    for table in tree.find_all(exp.Table):
        if table.catalog:
            assert f"{table.catalog}.{table.db}" == DATASET


@given(tokens=st.lists(st.sampled_from(_TOKENS), min_size=1, max_size=25))
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_token_soup_is_rejected_or_safely_compiled(tokens: list[str]) -> None:
    try:
        compiled = compile_sql(" ".join(tokens), ALICE)
    except QueryRejected:
        return
    _assert_safe(compiled)


# '0E' is an unparseable numeric literal: sqlglot accepts it, then raises ValueError
# from Literal.to_py during the grammar check (found by Hypothesis, T08-F1).
@example(name="0E")
@example(name="1e")
@given(name=st.text(min_size=1, max_size=20))
@settings(max_examples=200, deadline=None)
def test_arbitrary_identifiers_resolve_only_to_published_fields(name: str) -> None:
    for query in (
        f"SELECT {name} FROM customers",
        f"SELECT `{name}` FROM customers",
        f"SELECT c.{name} FROM customers c",
    ):
        try:
            compiled = compile_sql(query, ALICE)
        except QueryRejected:
            continue
        _assert_safe(compiled)
