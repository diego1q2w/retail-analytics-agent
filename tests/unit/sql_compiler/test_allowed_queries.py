"""Supported analytical queries compile and return only permitted data.

Results come from the DuckDB oracle over the synthetic fixture. Alice may see
products 1 and 3; Bob product 2. Order 100 mixes product 1 (30) and 2 (70).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime
from typing import Any

import duckdb
import pytest
import sqlglot
from sqlglot import exp

from tests.unit.sql_compiler.support import (
    ALICE,
    BOB,
    VERSION,
    compile_sql,
    database,
    execute,
    run,
)


@pytest.fixture
def db() -> Iterator[duckdb.DuckDBPyConnection]:
    connection = database()
    yield connection
    connection.close()


ALLOWED: list[tuple[str, list[tuple[Any, ...]]]] = [
    # --- ported from the feasibility experiment ---
    (
        "SELECT product_id,SUM(sale_amount) AS total FROM sales_items "
        "GROUP BY product_id ORDER BY product_id",
        [(1, 60.0)],
    ),
    (
        "SELECT SUM(sale_amount) AS revenue FROM sales_items WHERE "
        "item_status='Complete' AND ordered_date>=DATE '2026-09-01' "
        "AND ordered_date<DATE '2026-10-01'",
        [(30.0,)],
    ),
    (
        "SELECT s.product_id,p.category,SUM(s.sale_amount) AS total FROM sales_items s "
        "JOIN products p ON s.product_id=p.product_id GROUP BY s.product_id,p.category",
        [(1, "Clothing", 60.0)],
    ),
    (
        "SELECT s.sale_amount,o.visible_item_count FROM sales_items s JOIN orders o "
        "ON s.order_ref=o.order_ref WHERE s.ordered_date=DATE '2026-09-10'",
        [(30.0, 1)],
    ),
    (
        "SELECT c.customer_ref,SUM(s.sale_amount) AS total FROM sales_items s "
        "LEFT JOIN customers c ON c.customer_ref=s.customer_ref "
        "GROUP BY c.customer_ref",
        [("ref-customer_ref-10", 60.0)],
    ),
    (
        "SELECT product_id,product_name FROM products ORDER BY product_id",
        [(1, "Product A"), (3, "Unsold allowed product")],
    ),
    ("SELECT customer_ref FROM customers", [("ref-customer_ref-10",)]),
    (
        "SELECT COUNT(*) AS items,COUNT(DISTINCT order_ref) AS orders FROM sales_items",
        [(3, 3)],
    ),
    (
        "WITH monthly AS (SELECT product_id,SUM(sale_amount) AS total FROM sales_items "
        "GROUP BY product_id) SELECT total FROM monthly WHERE total>50",
        [(60.0,)],
    ),
    (
        "SELECT SUM(x.sale_amount) AS total "
        "FROM (SELECT sale_amount FROM sales_items) x",
        [(60.0,)],
    ),
    (
        "WITH sales_items AS (SELECT product_id,sale_amount FROM sales_items) "
        "SELECT SUM(sale_amount) AS total FROM sales_items",
        [(60.0,)],
    ),
    (
        "WITH products AS (SELECT product_id FROM products WHERE product_id=1), "
        "chosen AS (SELECT product_id FROM products) SELECT product_id FROM chosen",
        [(1,)],
    ),
    (
        "WITH customers AS (SELECT product_id AS email FROM products) "
        "SELECT email FROM customers ORDER BY email",
        [(1,), (3,)],
    ),
    (
        "SELECT product_id FROM products WHERE product_id IN "
        "(SELECT product_id FROM sales_items) ORDER BY product_id",
        [(1,)],
    ),
    ("SELECT (SELECT SUM(sale_amount) FROM sales_items) AS total", [(60.0,)]),
    (
        "SELECT DATE_TRUNC(ordered_date,MONTH) AS month,SUM(sale_amount) AS total "
        "FROM sales_items GROUP BY month ORDER BY month",
        [(date(2026, 8, 1), 10.0), (date(2026, 9, 1), 50.0)],
    ),
    (
        "SELECT SAFE_DIVIDE(SUM(sale_amount),COUNT(DISTINCT order_ref)) AS average "
        "FROM sales_items",
        [(20.0,)],
    ),
    ("SELECT 'DROP TABLE users' AS label", [("DROP TABLE users",)]),
    (
        "SELECT sale_amount AS email FROM sales_items ORDER BY email",
        [(10.0,), (20.0,), (30.0,)],
    ),
    ("SELECT product_id FROM products ORDER BY product_id LIMIT 1", [(1,)]),
    (
        "SELECT product_id,product_name FROM products ORDER BY 1 DESC",
        [(3, "Unsold allowed product"), (1, "Product A")],
    ),
    ("SELECT COUNT(*) FROM sales_items HAVING COUNT(*)>0", [(3,)]),
    (
        "SELECT CAST(product_id AS STRING) AS product FROM products ORDER BY product",
        [("1",), ("3",)],
    ),
    ("SELECT COALESCE(SUM(sale_amount),0) AS total FROM sales_items", [(60.0,)]),
    (
        "SELECT product_id FROM products WHERE product_id IN (1,3) ORDER BY product_id",
        [(1,), (3,)],
    ),
    (
        "SELECT product_id FROM products WHERE product_id IS NOT NULL "
        "ORDER BY product_id",
        [(1,), (3,)],
    ),
    (
        "WITH products AS (SELECT sale_amount FROM sales_items) "
        "SELECT SUM(sale_amount) AS total FROM products",
        [(60.0,)],
    ),
    ("SELECT product_id FROM products ORDER BY 1 LIMIT 0", []),
    # --- extensions: demographics, declared joins and the wider grammar ---
    (
        "SELECT c.state, c.age_band, COUNT(DISTINCT s.customer_ref) AS buyers, "
        "SUM(s.sale_amount) AS total FROM sales_items s JOIN customers c "
        "ON s.customer_ref = c.customer_ref GROUP BY c.state, c.age_band",
        [("CA", "under 30", 1, 60.0)],
    ),
    ("SELECT country, state, age_band FROM customers", [("US", "CA", "under 30")]),
    (
        "SELECT o.order_ref, c.state FROM orders o JOIN customers c "
        "ON o.customer_ref = c.customer_ref ORDER BY o.order_ref",
        [
            ("ref-order_ref-100", "CA"),
            ("ref-order_ref-101", "CA"),
            ("ref-order_ref-103", "CA"),
        ],
    ),
    (
        "SELECT p.brand, c.age_band, SUM(s.sale_amount) AS total FROM sales_items s "
        "JOIN products p ON s.product_id = p.product_id "
        "LEFT JOIN customers c ON s.customer_ref = c.customer_ref "
        "GROUP BY p.brand, c.age_band",
        [("Alpha", "under 30", 60.0)],
    ),
    (
        "SELECT s.item_ref FROM sales_items s "
        "JOIN orders o ON s.order_ref = o.order_ref "
        "JOIN customers c ON o.customer_ref = c.customer_ref "
        "WHERE c.state = 'CA' AND o.visible_item_count = 1 ORDER BY s.item_ref",
        [("ref-item_ref-1000",), ("ref-item_ref-1002",), ("ref-item_ref-1004",)],
    ),
    (
        "SELECT CASE WHEN sale_amount >= 25 THEN 'large' ELSE 'small' END AS size, "
        "COUNTIF(item_status = 'Complete') AS completed FROM sales_items "
        "GROUP BY size ORDER BY size",
        [("large", 1), ("small", 1)],
    ),
    (
        "SELECT EXTRACT(YEAR FROM ordered_date) AS y, "
        "DATE_DIFF(DATE '2026-10-01', MIN(ordered_date), DAY) AS age_days "
        "FROM sales_items GROUP BY y",
        [(2026, 57)],
    ),
    (
        "SELECT COUNT(*) AS n FROM sales_items "
        "WHERE ordered_date >= DATE_SUB(DATE '2026-09-15', INTERVAL 1 MONTH) "
        "AND ordered_date < DATE_ADD(DATE '2026-09-11', INTERVAL 1 DAY)",
        [(1,)],
    ),
    (
        "SELECT LOWER(category) AS c, UPPER(brand) AS b FROM products "
        "WHERE product_name LIKE 'Product%'",
        [("clothing", "ALPHA")],
    ),
    (
        "SELECT IF(product_id = 1, 'one', 'other') AS label, ROUND(catalog_price, 1) "
        "AS price, ABS(catalog_price - 20) AS distance, NULLIF(product_id, 3) AS id "
        "FROM products ORDER BY label",
        [("one", 30.0, 10.0, 1), ("other", 15.0, 5.0, None)],
    ),
    (
        "SELECT MIN(ordered_date) AS first, MAX(ordered_date) AS last, "
        "AVG(sale_amount) AS mean FROM sales_items",
        [(date(2026, 8, 5), date(2026, 9, 12), 20.0)],
    ),
    (
        "SELECT product_id FROM products WHERE NOT product_id BETWEEN 2 AND 5 "
        "OR product_name NOT LIKE '%A'",
        [(1,), (3,)],
    ),
    (
        "SELECT product_id FROM products p WHERE EXISTS "
        "(SELECT s.item_ref FROM sales_items s WHERE s.sale_amount > 25)",
        [(1,), (3,)],
    ),
    (
        "SELECT -sale_amount * 2 + 1 AS x FROM sales_items WHERE sale_amount = 10",
        [(-19.0,)],
    ),
    (
        "SELECT DISTINCT item_status FROM sales_items ORDER BY item_status",
        [("Complete",), ("Shipped",)],
    ),
    (
        "SELECT product_id /* only my products */ FROM products -- trailing\n"
        "ORDER BY product_id;",
        [(1,), (3,)],
    ),
    (
        "SELECT `product_id` FROM `products` WHERE TRUE AND product_id IS NOT NULL "
        "ORDER BY product_id",
        [(1,), (3,)],
    ),
]


@pytest.mark.parametrize(("query", "expected"), ALLOWED)
def test_allowed_query_returns_only_permitted_rows(
    db: duckdb.DuckDBPyConnection, query: str, expected: list[tuple[Any, ...]]
) -> None:
    compiled = compile_sql(query, ALICE)
    rows = [tuple(_normalize(v) for v in row) for row in execute(db, compiled)]
    assert rows == expected
    assert compiled.catalog_version == 1
    assert compiled.entitlement_version == VERSION
    reparsed = sqlglot.parse(compiled.sql, read="bigquery")
    assert len(reparsed) == 1 and isinstance(reparsed[0], exp.Select)


def _normalize(value: Any) -> Any:
    # DuckDB returns a timestamp for DATE_TRUNC over a date; BigQuery a date.
    return value.date() if isinstance(value, datetime) else value


def test_mixed_order_is_split_between_executives(db: duckdb.DuckDBPyConnection) -> None:
    query = (
        "SELECT SUM(sale_amount) AS total, COUNT(*) AS items, "
        "COUNT(DISTINCT order_ref) AS orders FROM sales_items "
        "WHERE ordered_date = DATE '2026-09-10'"
    )
    assert run(db, query, ALICE) == [(30.0, 1, 1)]
    assert run(db, query, BOB) == [(70.0, 1, 1)]


def test_order_counts_cover_only_permitted_items(db: duckdb.DuckDBPyConnection) -> None:
    query = "SELECT order_ref, visible_item_count FROM orders ORDER BY order_ref"
    assert run(db, query, ALICE) == [
        ("ref-order_ref-100", 1),
        ("ref-order_ref-101", 1),
        ("ref-order_ref-103", 1),
    ]
    assert run(db, query, BOB) == [("ref-order_ref-100", 1), ("ref-order_ref-102", 1)]
    both = run(db, query, ALICE.__class__(frozenset({"1", "2", "3"}), VERSION))
    assert ("ref-order_ref-100", 2) in both


def test_customers_are_reached_only_through_permitted_items(
    db: duckdb.DuckDBPyConnection,
) -> None:
    query = "SELECT customer_ref, state FROM customers ORDER BY customer_ref"
    assert run(db, query, ALICE) == [("ref-customer_ref-10", "CA")]
    assert run(db, query, BOB) == [
        ("ref-customer_ref-10", "CA"),
        ("ref-customer_ref-20", "NY"),
    ]


def test_products_include_unsold_permitted_products_only(
    db: duckdb.DuckDBPyConnection,
) -> None:
    query = "SELECT product_id FROM products ORDER BY product_id"
    assert run(db, query, ALICE) == [(1,), (3,)]
    assert run(db, query, BOB) == [(2,)]


def test_scope_is_applied_before_aggregation_in_ctes_and_subqueries(
    db: duckdb.DuckDBPyConnection,
) -> None:
    queries = [
        "WITH t AS (SELECT order_ref, SUM(sale_amount) AS v FROM sales_items "
        "GROUP BY order_ref) SELECT MAX(v) AS m FROM t",
        "SELECT MAX(v) AS m FROM (SELECT order_ref, SUM(sale_amount) AS v "
        "FROM sales_items GROUP BY order_ref) x",
        "SELECT (SELECT MAX(sale_amount) FROM sales_items) AS m",
    ]
    for query in queries:
        assert run(db, query, ALICE) == [(30.0,)]
        assert run(db, query, BOB) == [(90.0,)]
