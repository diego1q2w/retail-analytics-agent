# ruff: noqa: S608
# Constant table names; every value is a bound parameter.
"""Synthetic reference fixture ``golden-seed-fixture/1`` for the Golden seeds.

Invented rows, not a sample of any real dataset: eight fictional products,
twelve placeholder customers and a few dozen orders across June to early
October 2026. The seed reports quote figures that come from this fixture, and
``tests/unit/test_golden_seeds.py`` recomputes every one two independent ways
(SQL through the compiler and DuckDB, and plain Python over these rows).

Boundary cases are deliberate: orders on the first and last day of a window,
every non-complete status, a cancelled and a returned item, a product that
never sells, and a partial current month. All rows belong to the single
permitted scope ``SCOPE`` (every fixture product).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import duckdb

from retail_analytics.domain.access import ProductScope
from retail_analytics.domain.metric_evaluation import SalesItem

FIXTURE_ID = "golden-seed-fixture/1"
# Last observed day is 2026-10-07: the 8th is the partial current day.
AS_OF = date(2026, 10, 8)
RAW = '"bigquery-public-data".thelook_ecommerce.'
SCOPE = ProductScope(frozenset(str(p) for p in range(1, 9)), 1)


@dataclass(frozen=True)
class Product:
    product_id: int
    name: str
    category: str
    brand: str
    department: str
    retail_price: float


@dataclass(frozen=True)
class Customer:
    customer_id: int
    state: str
    country: str
    age: int


@dataclass(frozen=True)
class Line:
    product_id: int
    status: str
    price: str


@dataclass(frozen=True)
class Order:
    order_id: int
    customer_id: int
    day: date
    lines: tuple[Line, ...]


PRODUCTS = (
    Product(1, "Alpha Tee", "Tops", "Alpha", "Women", 25.0),
    Product(2, "Alpha Jacket", "Outerwear", "Alpha", "Women", 80.0),
    Product(3, "Beta Jeans", "Bottoms", "Beta", "Men", 60.0),
    Product(4, "Beta Shirt", "Tops", "Beta", "Men", 40.0),
    Product(5, "Gamma Dress", "Dresses", "Gamma", "Women", 90.0),
    Product(6, "Gamma Scarf", "Accessories", "Gamma", "Women", 15.0),
    Product(7, "Delta Boots", "Footwear", "Delta", "Men", 120.0),
    Product(8, "Delta Cap", "Accessories", "Delta", "Men", 20.0),
)

CUSTOMERS = (
    Customer(1, "California", "United States", 24),
    Customer(2, "California", "United States", 35),
    Customer(3, "New York", "United States", 52),
    Customer(4, "New York", "United States", 28),
    Customer(5, "Texas", "United States", 41),
    Customer(6, "Texas", "United States", 63),
    Customer(7, "Washington", "United States", 33),
    Customer(8, "Washington", "United States", 22),
    Customer(9, "Berlin", "Germany", 45),
    Customer(10, "Bavaria", "Germany", 58),
    Customer(11, "California", "United States", 47),
    Customer(12, "Florida", "United States", 29),
)


def _order(
    order_id: int, customer: int, day: str, *lines: tuple[int, str, str]
) -> Order:
    return Order(
        order_id,
        customer,
        date.fromisoformat(day),
        tuple(Line(p, s, price) for p, s, price in lines),
    )


C, R, X, S, P = "Complete", "Returned", "Cancelled", "Shipped", "Processing"

ORDERS = (
    # June (outside the three-month trend window)
    _order(29, 12, "2026-06-20", (3, C, "60")),
    _order(30, 8, "2026-06-28", (4, C, "40"), (6, C, "15")),
    # July
    _order(1, 1, "2026-07-03", (1, C, "22"), (6, C, "14")),
    _order(2, 2, "2026-07-09", (2, C, "75")),
    _order(3, 3, "2026-07-15", (3, C, "55"), (4, C, "38")),
    _order(4, 5, "2026-07-20", (5, C, "85")),
    _order(5, 7, "2026-07-28", (7, C, "110"), (6, C, "14")),
    _order(6, 4, "2026-07-30", (1, R, "22")),
    # August
    _order(7, 1, "2026-08-02", (5, C, "85"), (1, C, "24")),
    _order(8, 6, "2026-08-08", (3, C, "58")),
    _order(9, 9, "2026-08-12", (7, C, "115"), (4, C, "40")),
    _order(13, 11, "2026-08-14", (4, X, "40")),
    _order(10, 2, "2026-08-19", (2, C, "78"), (6, C, "15")),
    _order(11, 8, "2026-08-25", (1, C, "23")),
    _order(12, 10, "2026-08-31", (5, C, "88")),
    # September
    _order(14, 3, "2026-09-01", (2, C, "80")),
    _order(15, 5, "2026-09-04", (3, C, "60"), (1, C, "25")),
    _order(16, 12, "2026-09-06", (6, C, "15")),
    _order(17, 1, "2026-09-10", (5, C, "90")),
    _order(23, 11, "2026-09-12", (5, P, "90")),
    _order(18, 7, "2026-09-15", (4, C, "40")),
    _order(22, 6, "2026-09-18", (2, R, "80")),
    _order(19, 4, "2026-09-21", (1, C, "25"), (6, C, "15")),
    _order(20, 9, "2026-09-27", (7, C, "120")),
    _order(21, 2, "2026-09-30", (3, S, "60")),
    # October: the 8th is the partial current day
    _order(24, 1, "2026-10-01", (1, C, "25"), (5, C, "90")),
    _order(25, 5, "2026-10-03", (6, C, "15")),
    _order(26, 7, "2026-10-06", (2, C, "80")),
    _order(27, 3, "2026-10-07", (4, C, "40")),
    _order(28, 2, "2026-10-08", (1, C, "25")),
)


def sales_items() -> list[SalesItem]:
    """The ``sales_items`` view of the fixture, for the reference semantics."""
    items = []
    for order in ORDERS:
        for index, line in enumerate(order.lines, start=1):
            items.append(
                SalesItem(
                    item_ref=f"item-{order.order_id}-{index}",
                    order_ref=f"order-{order.order_id}",
                    customer_ref=f"customer-{order.customer_id}",
                    product_id=str(line.product_id),
                    item_status=line.status,
                    ordered_date=order.day,
                    sale_amount=Decimal(line.price),
                )
            )
    return items


def database() -> duckdb.DuckDBPyConnection:
    """The four source tables under the names the compiler binds to."""
    c = duckdb.connect(":memory:")
    c.execute("ATTACH ':memory:' AS \"bigquery-public-data\"")
    c.execute('CREATE SCHEMA "bigquery-public-data".thelook_ecommerce')
    c.execute(
        f"CREATE TABLE {RAW}products(id BIGINT PRIMARY KEY, name VARCHAR, "
        "category VARCHAR, brand VARCHAR, department VARCHAR, "
        "retail_price DOUBLE, cost DOUBLE)"
    )
    for p in PRODUCTS:
        c.execute(
            f"INSERT INTO {RAW}products VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                p.product_id,
                p.name,
                p.category,
                p.brand,
                p.department,
                p.retail_price,
                1,
            ],
        )
    c.execute(
        f"CREATE TABLE {RAW}users(id BIGINT PRIMARY KEY, first_name VARCHAR, "
        "last_name VARCHAR, email VARCHAR, age BIGINT, street_address VARCHAR, "
        "city VARCHAR, state VARCHAR, country VARCHAR)"
    )
    for u in CUSTOMERS:
        c.execute(
            f"INSERT INTO {RAW}users VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                u.customer_id,
                f"Placeholder{u.customer_id}",
                "Fixture",
                f"fixture{u.customer_id}@example.invalid",
                u.age,
                "n/a",
                "n/a",
                u.state,
                u.country,
            ],
        )
    c.execute(
        f"CREATE TABLE {RAW}orders(order_id BIGINT PRIMARY KEY, user_id BIGINT, "
        "created_at TIMESTAMP, num_of_item BIGINT, status VARCHAR)"
    )
    c.execute(
        f"CREATE TABLE {RAW}order_items(id BIGINT PRIMARY KEY, order_id BIGINT, "
        "user_id BIGINT, product_id BIGINT, status VARCHAR, sale_price DOUBLE)"
    )
    item_id = 1000
    for o in ORDERS:
        statuses = {line.status for line in o.lines}
        c.execute(
            f"INSERT INTO {RAW}orders VALUES (?, ?, ?, ?, ?)",
            [
                o.order_id,
                o.customer_id,
                f"{o.day.isoformat()} 12:00:00",
                len(o.lines),
                "Complete" if "Complete" in statuses else sorted(statuses)[0],
            ],
        )
        for line in o.lines:
            item_id += 1
            c.execute(
                f"INSERT INTO {RAW}order_items VALUES (?, ?, ?, ?, ?, ?)",
                [
                    item_id,
                    o.order_id,
                    o.customer_id,
                    line.product_id,
                    line.status,
                    float(line.price),
                ],
            )
    return c
