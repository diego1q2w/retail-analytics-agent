"""Logical relations and fields exposed by the analytical data contract (v0.2 §3).

This mirrors the draft contract so metric definitions can be validated without
the SQL compiler. Source mappings are provisional and pending live verification
(T34); schema discovery (T07) owns the discovered catalog and may replace this
reference with a richer one.
"""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType


class LogicalRelation(StrEnum):
    SALES_ITEMS = "sales_items"
    PRODUCTS = "products"
    ORDERS = "orders"
    CUSTOMERS = "customers"


LOGICAL_FIELDS: MappingProxyType[LogicalRelation, frozenset[str]] = MappingProxyType(
    {
        LogicalRelation.SALES_ITEMS: frozenset(
            {
                "item_ref",
                "order_ref",
                "customer_ref",
                "product_id",
                "item_status",
                "ordered_date",
                "sale_amount",
            }
        ),
        LogicalRelation.PRODUCTS: frozenset(
            {
                "product_id",
                "product_name",
                "category",
                "brand",
                "department",
                "catalog_price",
            }
        ),
        LogicalRelation.ORDERS: frozenset(
            {"order_ref", "customer_ref", "ordered_date", "visible_item_count"}
        ),
        LogicalRelation.CUSTOMERS: frozenset(
            {"customer_ref", "country", "state", "age_band"}
        ),
    }
)
