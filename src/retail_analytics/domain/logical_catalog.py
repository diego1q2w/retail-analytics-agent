"""Catalog v1: reviewed logical relations over the public retail dataset.

Mirrors the analytical data contract (v0.2 section 3). Source mappings are
checked against live metadata at runtime (drift evaluation) and by the opt-in
live metadata test; a new source column is never published by being present.
"""

from __future__ import annotations

from retail_analytics.domain.catalog import (
    Cardinality,
    Derivation,
    FieldDefinition,
    FieldType,
    JoinDefinition,
    LogicalCatalog,
    RelationDefinition,
    SourceColumnRef,
    SourceType,
)

CATALOG_VERSION = 1

_INT = frozenset({SourceType.INT64})
_STR = frozenset({SourceType.STRING})
_NUM = frozenset({SourceType.FLOAT64, SourceType.NUMERIC})
_TS = frozenset({SourceType.TIMESTAMP, SourceType.DATE})
_KEY = frozenset({SourceType.INT64, SourceType.STRING})

_ITEMS = "order_items"
_ORDERS = "orders"
_PRODUCTS = "products"
_USERS = "users"


def _col(table: str, column: str, accepted: frozenset[SourceType]) -> SourceColumnRef:
    return SourceColumnRef(table, column, accepted)


def _direct(
    name: str,
    type_: FieldType,
    description: str,
    source: SourceColumnRef,
    *,
    essential: bool = False,
) -> FieldDefinition:
    return FieldDefinition(
        name, type_, description, Derivation.DIRECT, (source,), essential
    )


def _ref(name: str, description: str, *sources: SourceColumnRef) -> FieldDefinition:
    return FieldDefinition(
        name,
        FieldType.REFERENCE,
        description,
        Derivation.OPAQUE_REFERENCE,
        sources,
        essential=True,
    )


def default_logical_catalog() -> LogicalCatalog:
    sales_items = RelationDefinition(
        name="sales_items",
        description="Items sold within your permitted products.",
        grain="one permitted order item",
        fields=(
            _ref("item_ref", "Opaque reference to the item.", _col(_ITEMS, "id", _KEY)),
            _ref(
                "order_ref",
                "Opaque reference to the order containing the item.",
                _col(_ITEMS, "order_id", _KEY),
            ),
            _ref(
                "customer_ref",
                "Opaque reference to the purchasing customer.",
                _col(_ITEMS, "user_id", _KEY),
            ),
            _direct(
                "product_id",
                FieldType.INTEGER,
                "Product identifier; joins to products.",
                _col(_ITEMS, "product_id", _INT),
                essential=True,
            ),
            _direct(
                "item_status",
                FieldType.STRING,
                "Current item status (for example Complete or Returned).",
                _col(_ITEMS, "status", _STR),
            ),
            FieldDefinition(
                "ordered_date",
                FieldType.DATE,
                "UTC date on which the order was placed.",
                Derivation.DATE_OF_TIMESTAMP,
                (_col(_ORDERS, "created_at", _TS),),
                essential=True,
            ),
            _direct(
                "sale_amount",
                FieldType.NUMBER,
                "Sale price of the item in the dataset currency.",
                _col(_ITEMS, "sale_price", _NUM),
            ),
        ),
        joins=(
            JoinDefinition(
                "product_id", "products", "product_id", Cardinality.MANY_TO_ONE
            ),
            JoinDefinition("order_ref", "orders", "order_ref", Cardinality.MANY_TO_ONE),
            JoinDefinition(
                "customer_ref", "customers", "customer_ref", Cardinality.MANY_TO_ONE
            ),
        ),
    )
    products = RelationDefinition(
        name="products",
        description="Products you are permitted to analyze, including unsold ones.",
        grain="one permitted product",
        fields=(
            _direct(
                "product_id",
                FieldType.INTEGER,
                "Product identifier.",
                _col(_PRODUCTS, "id", _INT),
                essential=True,
            ),
            _direct(
                "product_name",
                FieldType.STRING,
                "Product name.",
                _col(_PRODUCTS, "name", _STR),
            ),
            _direct(
                "category",
                FieldType.STRING,
                "Product category.",
                _col(_PRODUCTS, "category", _STR),
            ),
            _direct(
                "brand",
                FieldType.STRING,
                "Product brand.",
                _col(_PRODUCTS, "brand", _STR),
            ),
            _direct(
                "department",
                FieldType.STRING,
                "Product department.",
                _col(_PRODUCTS, "department", _STR),
            ),
            _direct(
                "catalog_price",
                FieldType.NUMBER,
                "List price of the product (not a realized sale amount).",
                _col(_PRODUCTS, "retail_price", _NUM),
            ),
        ),
    )
    orders = RelationDefinition(
        name="orders",
        description="Orders containing at least one permitted item.",
        grain="one order, restricted to its permitted items",
        fields=(
            _ref(
                "order_ref",
                "Opaque reference to the order.",
                _col(_ORDERS, "order_id", _KEY),
            ),
            _ref(
                "customer_ref",
                "Opaque reference to the ordering customer.",
                _col(_ORDERS, "user_id", _KEY),
            ),
            FieldDefinition(
                "ordered_date",
                FieldType.DATE,
                "UTC date on which the order was placed.",
                Derivation.DATE_OF_TIMESTAMP,
                (_col(_ORDERS, "created_at", _TS),),
                essential=True,
            ),
            FieldDefinition(
                "visible_item_count",
                FieldType.INTEGER,
                "Number of items in the order within your permitted products.",
                Derivation.PERMITTED_ITEM_COUNT,
                (_col(_ITEMS, "id", _KEY), _col(_ITEMS, "order_id", _KEY)),
            ),
        ),
        joins=(
            JoinDefinition(
                "customer_ref", "customers", "customer_ref", Cardinality.MANY_TO_ONE
            ),
        ),
    )
    customers = RelationDefinition(
        name="customers",
        description="Customers with purchases in your permitted products.",
        grain="one customer reached through permitted items",
        fields=(
            _ref(
                "customer_ref",
                "Opaque reference to the customer.",
                _col(_USERS, "id", _KEY),
            ),
            _direct(
                "country",
                FieldType.STRING,
                "Customer country.",
                _col(_USERS, "country", _STR),
            ),
            _direct(
                "state",
                FieldType.STRING,
                "Customer state or region.",
                _col(_USERS, "state", _STR),
            ),
            FieldDefinition(
                "age_band",
                FieldType.STRING,
                "Age band derived by trusted code; exact age is unavailable.",
                Derivation.AGE_BAND,
                (_col(_USERS, "age", _INT),),
            ),
        ),
    )
    return LogicalCatalog(CATALOG_VERSION, (sales_items, products, orders, customers))
