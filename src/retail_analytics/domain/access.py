"""Server-side data scope of an executive."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProductScope:
    """Products an executive may see, resolved by trusted application code.

    An empty scope means no product data at all, never unrestricted access.
    """

    product_ids: frozenset[str]
    entitlement_version: int

    def __post_init__(self) -> None:
        if self.entitlement_version < 0:
            raise ValueError("entitlement_version must be non-negative")

    @property
    def is_empty(self) -> bool:
        return not self.product_ids
