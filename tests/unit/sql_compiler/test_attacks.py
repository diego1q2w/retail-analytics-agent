"""Adversarial queries: every one must be rejected before execution.

Grouped by the expected outcome class so a regression that downgrades a
permission failure into a generic one is caught too. Rejection messages must
never mention physical sources or unpublished fields.
"""

from __future__ import annotations

import pytest

from retail_analytics.application.query_compiler import QueryRejected
from retail_analytics.domain.operations import ToolErrorCode
from tests.unit.sql_compiler.support import ALICE, compile_sql

FIELD = ToolErrorCode.FIELD_UNAVAILABLE
UNSUPPORTED = ToolErrorCode.UNSUPPORTED_SQL
INVALID = ToolErrorCode.INVALID_QUERY
INPUT = ToolErrorCode.INVALID_INPUT

# Forbidden or unknown fields and relations, however they are reached.
FORBIDDEN_DATA = [
    # ported
    "SELECT product_id FROM products WHERE email IS NOT NULL",
    "SELECT product_id FROM products ORDER BY email",
    "SELECT COUNT(*) FROM customers HAVING MAX(email) IS NOT NULL",
    "WITH unused AS (SELECT email FROM users) SELECT product_id FROM products",
    "WITH orders AS (SELECT num_of_item FROM orders) SELECT num_of_item FROM orders",
    "SELECT product_id FROM `bigquery-public-data.thelook_ecommerce.products`",
    "SELECT email FROM users",
    "SELECT email FROM customers",
    "SELECT s.email FROM sales_items s",
    "WITH sales_items AS (SELECT email FROM users) SELECT email FROM sales_items",
    "WITH x AS (SELECT email FROM customers) SELECT email FROM x",
    "SELECT (SELECT email FROM users) AS hidden FROM products",
    "SELECT product_id FROM products WHERE EXISTS (SELECT email FROM users)",
    "SELECT num_of_item FROM orders",
    "SELECT status FROM orders",
    "SELECT state FROM customer_segments",
    "SELECT * FROM INFORMATION_SCHEMA.TABLES",
    "SELECT product_id FROM `products*`",
    "SELECT missing FROM products",
    # exact age, raw keys and source column names
    "SELECT age FROM customers",
    "SELECT c.age FROM customers c",
    "SELECT customer_ref FROM customers WHERE age = 41",
    "SELECT MAX(age) AS oldest FROM customers",
    "SELECT age_band FROM customers GROUP BY age_band HAVING MIN(age) > 40",
    "SELECT state FROM customers ORDER BY age",
    "SELECT COUNT(*) FROM customers GROUP BY age",
    "SELECT id FROM customers",
    "SELECT user_id FROM sales_items",
    "SELECT order_id FROM orders",
    "SELECT i.id FROM sales_items i",
    "SELECT sale_price FROM sales_items",
    "SELECT name FROM products",
    "SELECT cost FROM products",
    "SELECT retail_price FROM products",
    "SELECT created_at FROM orders",
    "SELECT first_name FROM customers",
    "SELECT last_name FROM customers",
    "SELECT street_address FROM customers",
    "SELECT postal_code FROM customers",
    "SELECT city FROM customers",
    "SELECT latitude, longitude FROM customers",
    # physical and metadata sources
    "SELECT id FROM thelook_ecommerce.users",
    "SELECT id FROM `bigquery-public-data`.thelook_ecommerce.users",
    "SELECT id FROM order_items",
    "SELECT customer_ref FROM customers c JOIN users u ON c.customer_ref = u.id",
    "SELECT table_name FROM `region-us`.INFORMATION_SCHEMA.TABLES",
    "SELECT product_id FROM products__history",
    # escapes, quoting, case and look-alike identifiers
    "SELECT `email` FROM customers",
    "SELECT `customers`.`email` FROM `customers`",
    "SELECT EMAIL FROM customers",
    "SELECT Email FROM Customers",
    "SELECT \uff45mail FROM customers",
    "SELECT `e``mail` FROM customers",
    "SELECT/**/email/**/FROM/**/customers",
    "SELECT customer_ref FROM customers -- \nWHERE email = 'x'",
    # whole-row and struct-style references
    "SELECT c FROM customers c",
    "SELECT c AS x FROM customers c",
    "SELECT COUNT(DISTINCT c) AS n FROM customers c",
    "SELECT x FROM (SELECT c AS x FROM customers c)",
    "SELECT state FROM customers c WHERE c IS NOT NULL",
    "SELECT s.customer_ref.raw FROM sales_items s",
    "SELECT a.b.c FROM customers a",
    # nested queries, CTE shadowing and resolution gaps
    "SELECT (SELECT (SELECT email FROM customers)) AS x",
    "SELECT x FROM (SELECT (SELECT age FROM customers LIMIT 1) AS x)",
    "WITH users AS (SELECT customer_ref FROM customers) SELECT email FROM users",
    "WITH customers AS (SELECT customer_ref FROM customers) SELECT age FROM customers",
    "WITH c AS (SELECT customer_ref, age_band FROM customers) SELECT c.age FROM c",
    "WITH customers AS (SELECT email FROM customers) SELECT email FROM customers",
    "SELECT x.email FROM (SELECT customer_ref AS email2 FROM customers) x",
    "SELECT state FROM customers GROUP BY state HAVING COUNT(email) > 0",
    "SELECT COUNT(*) AS n FROM sales_items HAVING MAX(sale_price) > 0",
    "SELECT product_id FROM products WHERE product_id IN (SELECT id FROM users)",
    "SELECT _policy_row FROM sales_items",
    "SELECT s._policy_row FROM sales_items s",
]

UNSUPPORTED_SQL = [
    # ported: statements, scripts and exports
    "DELETE FROM sales_items",
    "UPDATE sales_items SET sale_amount=0",
    "INSERT INTO sales_items(product_id) VALUES (1)",
    "CREATE TABLE copied AS SELECT product_id FROM products",
    "DROP TABLE products",
    "EXPORT DATA OPTIONS(uri='gs://elsewhere/*',format='CSV') AS "
    "SELECT product_id FROM products",
    "CALL some_procedure()",
    'EXECUTE IMMEDIATE "SELECT email FROM users"',
    "SELECT product_id FROM products; DELETE FROM products",
    "SELECT product_id FROM products /* comment */; SELECT email FROM users",
    # ported: grammar outside the subset
    'SELECT product_id FROM products PIVOT(COUNT(*) FOR category IN ("Clothing"))',
    "SELECT TO_JSON_STRING(p) FROM products p",
    "SELECT product_id FROM products UNION ALL SELECT id FROM "
    "`bigquery-public-data.thelook_ecommerce.products`",
    "SELECT * FROM sales_items",
    "SELECT s.* FROM sales_items s",
    "SELECT * EXCEPT(customer_ref) FROM sales_items",
    "SELECT COUNT(s.*) FROM sales_items s",
    "SELECT * FROM EXTERNAL_QUERY('connection','SELECT email FROM users')",
    "SELECT secret_dataset.remote_function(product_id) FROM products",
    "SELECT SESSION_USER()",
    "SELECT RAND() FROM sales_items",
    "SELECT ARRAY_AGG(customer_ref) FROM customers",
    "SELECT sale_amount / 0 FROM sales_items",
    "WITH RECURSIVE x AS (SELECT product_id FROM products) SELECT product_id FROM x",
    "SELECT product_id FROM products TABLESAMPLE SYSTEM (10 PERCENT)",
    "SELECT product_id, ROW_NUMBER() OVER () AS n FROM products",
    "SELECT product_id FROM products QUALIFY ROW_NUMBER() OVER ()=1",
    "SELECT product_id FROM products FOR SYSTEM_TIME AS OF CURRENT_TIMESTAMP()",
    "SELECT product_id FROM products WHERE product_id IN UNNEST(@_policy_products)",
    "SELECT @@project_id",
    # ported: joins outside the declared graph
    "SELECT s.sale_amount FROM sales_items s JOIN products p ON "
    "s.product_id=p.product_id JOIN products p2 ON s.product_id=p2.product_id",
    "SELECT s.sale_amount FROM sales_items s CROSS JOIN products p",
    "SELECT s.sale_amount FROM sales_items s, products p",
    "SELECT s.sale_amount FROM sales_items s JOIN products p ON 1=1",
    "SELECT s.sale_amount FROM sales_items s JOIN products p "
    "ON s.product_id!=p.product_id",
    "SELECT s.sale_amount FROM sales_items s JOIN products p "
    "ON s.product_id=p.product_id OR 1=1",
    "SELECT s.sale_amount FROM sales_items s JOIN sales_items t "
    "ON s.order_ref=t.order_ref",
    "SELECT s.sale_amount FROM sales_items s NATURAL JOIN products p",
    "SELECT s.sale_amount FROM sales_items s JOIN products p USING(product_id)",
    "SELECT s.sale_amount FROM sales_items s RIGHT JOIN products p "
    "ON s.product_id=p.product_id",
    "WITH p AS (SELECT product_id FROM products) SELECT s.sale_amount "
    "FROM sales_items s JOIN p ON s.product_id=p.product_id",
    "SELECT s.product_id FROM sales_items s WHERE EXISTS (SELECT p.product_id "
    "FROM products p WHERE p.product_id=s.product_id)",
    # extensions: more statements and scripting
    "MERGE products T USING products S ON T.product_id = S.product_id "
    "WHEN MATCHED THEN DELETE",
    "TRUNCATE TABLE products",
    "ALTER TABLE products ADD COLUMN x INT64",
    "GRANT `roles/bigquery.dataViewer` ON TABLE products TO 'user:x@example.invalid'",
    "BEGIN TRANSACTION",
    "DECLARE x INT64",
    "SET x = 1",
    "ASSERT (SELECT COUNT(*) FROM products) > 0",
    "CREATE TEMP FUNCTION f(x INT64) AS (x); SELECT f(product_id) FROM products",
    "SELECT 1 UNION ALL SELECT 2",
    "SELECT product_id FROM products INTERSECT DISTINCT "
    "SELECT product_id FROM products",
    "SELECT product_id FROM products EXCEPT DISTINCT SELECT product_id FROM products",
    # extensions: functions outside the allowlist
    "SELECT SAFE.SUBSTR(product_name, 1) FROM products",
    "SELECT NET.HOST('https://example.invalid')",
    "SELECT my_udf(product_id) FROM products",
    "SELECT `project.dataset.fn`(product_id) FROM products",
    "SELECT CURRENT_DATE()",
    "SELECT GENERATE_UUID()",
    "SELECT STRING_AGG(customer_ref) FROM customers",
    "SELECT CONCAT(state, country) FROM customers",
    "SELECT FARM_FINGERPRINT(customer_ref) FROM customers",
    "SELECT ERROR('boom')",
    "SELECT SAFE_CAST(product_id AS STRING) FROM products",
    "SELECT TO_HEX(SHA256(customer_ref)) AS h FROM customers",
    "SELECT ANY_VALUE(state) FROM customers",
    "SELECT ML.PREDICT(MODEL m, (SELECT product_id FROM products))",
    "SELECT AEAD.DECRYPT_STRING(k, c, a)",
    "SELECT ARRAY(SELECT product_id FROM products) AS a",
    "SELECT STRUCT(product_id AS a) AS s FROM products",
    "SELECT [1, 2] AS a",
    "SELECT x FROM UNNEST([1, 2]) AS x",
    "SELECT p.product_id FROM products p, UNNEST([1]) AS x",
    "SELECT product_id FROM products WHERE product_id IN UNNEST([1, 2])",
    "SELECT r'raw' AS r",
    "SELECT b'bytes' AS b",
    "SELECT 0x10 AS h",
    "SELECT JSON '{}' AS j",
    "SELECT INTERVAL 1 DAY AS i",
    "SELECT product_id FROM products LIMIT 5 OFFSET 2",
    "SELECT CAST(product_id AS BYTES) FROM products",
    "SELECT CAST(product_id AS STRUCT<a INT64>) FROM products",
    "SELECT DATE_TRUNC(ordered_date, HOUR) FROM sales_items",
    "SELECT DATE_TRUNC(ordered_date, WEEK(MONDAY)) FROM sales_items",
    "SELECT DATE_ADD(ordered_date, INTERVAL 100000 DAY) FROM sales_items",
    "SELECT DATE_ADD(ordered_date, INTERVAL @n DAY) FROM sales_items",
    "SELECT product_id FROM products WHERE product_name LIKE 'a!%' ESCAPE '!'",
    "SELECT product_id FROM products WINDOW w AS (ORDER BY product_id)",
    # extensions: joins
    "SELECT p.product_id FROM products p "
    "JOIN sales_items s ON s.product_id=p.product_id",
    "SELECT c.state FROM customers c "
    "JOIN sales_items s ON s.customer_ref=c.customer_ref",
    "SELECT s.item_ref FROM sales_items s JOIN orders o "
    "ON s.customer_ref = o.customer_ref",
    "SELECT s.item_ref FROM sales_items s "
    "JOIN customers c ON s.order_ref = c.customer_ref",
    "SELECT s.item_ref FROM sales_items s FULL OUTER JOIN customers c "
    "ON s.customer_ref = c.customer_ref",
    "SELECT s.item_ref FROM sales_items s JOIN customers c "
    "ON s.customer_ref = c.customer_ref AND c.state = 'CA'",
    "SELECT s.item_ref FROM sales_items s JOIN customers c "
    "ON c.customer_ref = c.customer_ref",
    "SELECT x.v FROM (SELECT customer_ref AS v FROM sales_items) x "
    "JOIN customers c ON x.v = c.customer_ref",
    "SELECT s.item_ref FROM sales_items s LEFT JOIN customers c "
    "ON s.customer_ref = c.customer_ref JOIN customers d "
    "ON s.customer_ref = d.customer_ref",
    # extensions: correlation hidden in other clauses
    "SELECT c.state, (SELECT COUNT(*) FROM sales_items s "
    "WHERE s.customer_ref = c.customer_ref) AS n FROM customers c",
    "SELECT c.state FROM customers c WHERE c.customer_ref IN "
    "(SELECT s.customer_ref FROM sales_items s WHERE s.customer_ref = c.customer_ref)",
    "WITH x AS (SELECT product_id FROM products) SELECT product_id FROM products",
]

INVALID_QUERIES = [
    "SELECT FROM products",
    "SELECT product_id AS x,product_id AS x FROM products",
    "SELECT product_id AS x, product_id AS X FROM products",
    "SELECT ( FROM products",
    "SELECT product_id FROM products AS _POLICY_HIDDEN",
    "WITH _policy_products AS (SELECT product_id FROM products) "
    "SELECT product_id FROM _policy_products",
    "SELECT product_id FROM products AS _value_0",
    "SELECT product_id FROM products LIMIT 1000000",
    "SELECT product_id FROM products LIMIT @n",
    "SELECT product_id FROM sales_items s JOIN products p ON s.product_id=p.product_id",
    "SELECT 1e400 AS x",
    "SELECT 99999999999999999999 AS x",
    "SELECT product_id FROM products WHERE DATE '2026-13-45' IS NOT NULL",
    "SELECT product_id FROM products ORDER BY",
    "SELECT product_id FROM products " + "WHERE " + "(" * 400 + "TRUE" + ")" * 400,
    "SELECT " + " + ".join(["product_id"] * 1500) + " AS x FROM products",
    "SELECT product_id FROM products WHERE product_name = '" + "x" * 20_000 + "'",
]

PARAMETER_TAMPERING = [
    "SELECT @_POLICY_PRODUCTS",
    "SELECT @_policy_products",
    "SELECT @_policy_product_ids",
    "SELECT @_value_0",
    "SELECT product_id FROM products WHERE product_id IN (@_policy_product_ids)",
]


def _rejected(query: str) -> QueryRejected:
    with pytest.raises(QueryRejected) as caught:
        compile_sql(query, ALICE)
    error = caught.value
    for leaked in ("bigquery-public-data", "thelook", "order_items", "users."):
        assert leaked not in error.message
    return error


@pytest.mark.parametrize("query", FORBIDDEN_DATA)
def test_forbidden_or_unknown_data_is_unavailable(query: str) -> None:
    error = _rejected(query)
    # Unpublished schema is never reported as merely malformed SQL, and never
    # as something a retry could fix by relaxing policy.
    assert error.code in (FIELD, UNSUPPORTED, INVALID), error.reason


@pytest.mark.parametrize("query", UNSUPPORTED_SQL)
def test_unsupported_constructs_fail_closed(query: str) -> None:
    error = _rejected(query)
    assert error.code in (UNSUPPORTED, INVALID, FIELD), error.reason


@pytest.mark.parametrize("query", INVALID_QUERIES)
def test_invalid_queries_are_rejected(query: str) -> None:
    error = _rejected(query)
    assert error.code in (INVALID, UNSUPPORTED, FIELD), error.reason


@pytest.mark.parametrize("query", PARAMETER_TAMPERING)
def test_policy_parameters_cannot_be_referenced(query: str) -> None:
    error = _rejected(query)
    assert error.code in (INVALID, UNSUPPORTED, INPUT), error.reason


@pytest.mark.parametrize(
    ("query", "code", "field"),
    [
        ("SELECT email FROM customers", FIELD, "email"),
        ("SELECT c.age FROM customers c", FIELD, "age"),
        ("SELECT state FROM customers ORDER BY age", FIELD, "age"),
        (
            "SELECT COUNT(*) FROM customers HAVING MAX(email) IS NOT NULL",
            FIELD,
            "email",
        ),
        ("SELECT id FROM order_items", FIELD, None),
        ("SELECT * FROM sales_items", UNSUPPORTED, None),
        ("DELETE FROM sales_items", UNSUPPORTED, None),
        (
            "SELECT s.item_ref FROM sales_items s CROSS JOIN products p",
            UNSUPPORTED,
            None,
        ),
    ],
)
def test_rejection_codes_are_specific(
    query: str, code: ToolErrorCode, field: str | None
) -> None:
    error = _rejected(query)
    assert error.code is code
    if field is not None:
        assert error.field == field
        assert error.correctable


def test_unknown_relation_reports_only_the_requested_name() -> None:
    error = _rejected("SELECT id FROM order_items")
    assert error.reason == "relation_unavailable"
    assert error.relation == "order_items"
    assert error.available_fields == ()


def test_wildcard_rejection_is_reformulable() -> None:
    error = _rejected("SELECT * FROM products")
    assert error.reason == "wildcard_projection"
    assert error.correctable
