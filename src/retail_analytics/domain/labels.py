"""Display-only fallbacks for missing product labels.

The source has products without a name or brand. Stored evidence keeps the
NULL (it is what the source said); only text shown to a person or a model
substitutes an explicit label. Product identity is always the product ID: two
unnamed products are two products, so a fallback is never used to group,
deduplicate or join, and the ID column is shown next to it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal

from retail_analytics.domain.evidence import EvidenceCell, EvidenceColumn, EvidenceTable

UNNAMED_PRODUCT = "Unnamed product"
UNKNOWN_BRAND = "Unknown brand"

_NAME_FIELDS = frozenset({"products.product_name"})
_BRAND_FIELDS = frozenset({"products.brand"})
_PRODUCT_ID_FIELDS = frozenset({"products.product_id", "sales_items.product_id"})
NULL_TEXT = "(none)"


def fallback_for(column: EvidenceColumn) -> str | None:
    """The label shown for a NULL in ``column``, or None for other columns.

    Applies only when every source field of the column is a product name or
    brand, so a NULL in an unrelated or mixed column is not relabelled.
    """
    sources = frozenset(column.sources)
    if not sources:
        return None
    if sources <= _NAME_FIELDS:
        return UNNAMED_PRODUCT
    if sources <= _BRAND_FIELDS:
        return UNKNOWN_BRAND
    return None


def has_product_id(columns: Sequence[EvidenceColumn]) -> bool:
    return any(frozenset(c.sources) & _PRODUCT_ID_FIELDS for c in columns)


def format_cell(value: EvidenceCell, fallback: str | None = None) -> str:
    """Text for one cell; NULL becomes ``fallback`` (or a neutral marker)."""
    if value is None:
        return fallback if fallback is not None else NULL_TEXT
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def present_rows(table: EvidenceTable) -> tuple[tuple[str, ...], ...]:
    """Every row as display text, with label fallbacks applied to NULLs.

    Rows are never merged or dropped: one stored row is one displayed row.
    """
    fallbacks = [fallback_for(c) for c in table.columns]
    return tuple(
        tuple(format_cell(v, f) for v, f in zip(row, fallbacks, strict=True))
        for row in table.rows
    )


def label_notes(table: EvidenceTable) -> tuple[str, ...]:
    """Disclosures for fallback labels used in ``table``.

    One note per fallback with how many rows show it (those rows stay separate
    rows or groups), and a warning when unnamed products cannot be told apart
    because the table has no product ID.
    """
    notes: list[str] = []
    unnamed_without_id = False
    for index, column in enumerate(table.columns):
        label = fallback_for(column)
        if label is None:
            continue
        missing = sum(1 for row in table.rows if row[index] is None)
        if not missing:
            continue
        kind = "name" if label == UNNAMED_PRODUCT else "brand"
        notes.append(
            f'{missing} row(s) show "{label}" because the source has no product '
            f"{kind} for them. They are shown as they are stored, never merged "
            "into another row or group."
        )
        unnamed_without_id |= label == UNNAMED_PRODUCT
    if unnamed_without_id and not has_product_id(table.columns):
        notes.append(
            f'Rows labelled "{UNNAMED_PRODUCT}" carry no product ID here, so '
            "different unnamed products cannot be told apart."
        )
    return tuple(notes)
