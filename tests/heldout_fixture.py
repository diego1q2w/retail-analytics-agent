"""Loader for the held-out analytical fixture ``heldout-fixture-1``.

The rows live in ``evaluation/heldout/fixture/*.json`` as plain data. Nothing
here imports the compiler, the metric catalog or the agent: expected values are
checked against this data by routes that do not share code with the code under
test (reference SQL in DuckDB, and plain Python over the rows).

All data is invented. Customer "names" and "emails" are placeholders on the
reserved ``example.invalid`` domain; they exist so privacy scenarios have
canary values that must never appear in an answer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import duckdb

ROOT: Final = Path(__file__).resolve().parents[1] / "evaluation"
HELDOUT: Final = ROOT / "heldout"
FIXTURE_DIR: Final = HELDOUT / "fixture"
REFERENCE_SQL_DIR: Final = HELDOUT / "reference-sql"
MANIFEST_PATH: Final = HELDOUT / "manifest.json"
SPLITS_PATH: Final = ROOT / "splits.json"
FIXTURE_ID: Final = "heldout-fixture-1"


@dataclass(frozen=True)
class Product:
    product_id: int
    name: str
    category: str
    brand: str
    department: str
    retail_price: Decimal


@dataclass(frozen=True)
class Customer:
    customer_id: int
    first_name: str
    last_name: str
    email: str
    state: str
    country: str
    age: int


@dataclass(frozen=True)
class Item:
    order_id: int
    customer_id: int
    ordered_at: datetime
    product_id: int
    status: str
    amount: Decimal


def _rows(name: str) -> list[dict[str, Any]]:
    data = json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))
    assert isinstance(data, list)
    return data


def products() -> tuple[Product, ...]:
    return tuple(
        Product(
            r["product_id"],
            r["name"],
            r["category"],
            r["brand"],
            r["department"],
            Decimal(str(r["retail_price"])),
        )
        for r in _rows("products.json")
    )


def customers() -> tuple[Customer, ...]:
    return tuple(
        Customer(
            r["customer_id"],
            r["first_name"],
            r["last_name"],
            r["email"],
            r["state"],
            r["country"],
            r["age"],
        )
        for r in _rows("customers.json")
    )


def items() -> tuple[Item, ...]:
    return tuple(
        Item(
            o["order_id"],
            o["customer_id"],
            datetime.fromisoformat(o["created_at"]),
            i["product_id"],
            i["status"],
            Decimal(str(i["sale_price"])),
        )
        for o in _rows("orders.json")
        for i in o["items"]
    )


def database() -> duckdb.DuckDBPyConnection:
    """The four source tables in schema ``thelook``, exact decimal amounts."""
    c = duckdb.connect(":memory:")
    c.execute("CREATE SCHEMA thelook")
    c.execute(
        "CREATE TABLE thelook.products(id BIGINT, name VARCHAR, category VARCHAR, "
        "brand VARCHAR, department VARCHAR, retail_price DECIMAL(10,2))"
    )
    c.execute(
        "CREATE TABLE thelook.users(id BIGINT, first_name VARCHAR, "
        "last_name VARCHAR, email VARCHAR, age BIGINT, state VARCHAR, "
        "country VARCHAR)"
    )
    c.execute(
        "CREATE TABLE thelook.orders(order_id BIGINT, user_id BIGINT, "
        "created_at TIMESTAMP)"
    )
    c.execute(
        "CREATE TABLE thelook.order_items(id BIGINT, order_id BIGINT, "
        "user_id BIGINT, product_id BIGINT, status VARCHAR, "
        "sale_price DECIMAL(10,2))"
    )
    for p in products():
        c.execute(
            "INSERT INTO thelook.products VALUES (?, ?, ?, ?, ?, ?)",
            [p.product_id, p.name, p.category, p.brand, p.department, p.retail_price],
        )
    for u in customers():
        c.execute(
            "INSERT INTO thelook.users VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                u.customer_id,
                u.first_name,
                u.last_name,
                u.email,
                u.age,
                u.state,
                u.country,
            ],
        )
    item_id = 0
    for o in _rows("orders.json"):
        c.execute(
            "INSERT INTO thelook.orders VALUES (?, ?, ?)",
            [o["order_id"], o["customer_id"], o["created_at"].replace("T", " ")],
        )
        for i in o["items"]:
            item_id += 1
            c.execute(
                "INSERT INTO thelook.order_items VALUES (?, ?, ?, ?, ?, ?)",
                [
                    item_id,
                    o["order_id"],
                    o["customer_id"],
                    i["product_id"],
                    i["status"],
                    Decimal(str(i["sale_price"])),
                ],
            )
    return c
