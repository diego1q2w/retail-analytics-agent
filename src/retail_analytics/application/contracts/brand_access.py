"""Records of brand-based access: the trusted catalog snapshot and results.

A manager's assigned brands resolve to products through ``ProductBrandCatalog``
(``products.brand`` read from the warehouse by trusted code). The resolved
products join the explicit product grants in ``ExecutiveAccess.product_ids``,
so every existing product-scope check applies unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from retail_analytics.domain.access import (
    ExecutiveAccess,
    is_valid_brand,
    is_valid_product_id,
    product_brand_digest,
)


@dataclass(frozen=True, slots=True)
class ProductBrandCatalog:
    """Product-to-brand snapshot from the trusted catalog.

    ``brands`` maps product ID to the exact catalog brand. Products without a
    usable brand (missing, blank or padded) are only counted: no brand
    assignment ever grants them.
    """

    brands: Mapping[str, str]
    source_ref: str
    products_without_brand: int = 0

    def __post_init__(self) -> None:
        if not self.source_ref:
            raise ValueError("source_ref is required")
        if self.products_without_brand < 0:
            raise ValueError("products_without_brand must be non-negative")
        for product_id, brand in self.brands.items():
            if not is_valid_product_id(product_id) or not is_valid_brand(brand):
                raise ValueError("catalog holds an invalid product or brand")

    @property
    def digest(self) -> str:
        return product_brand_digest(self.brands)


@dataclass(frozen=True, slots=True)
class ExecutiveScopeChange:
    """An executive whose resolved products changed in a catalog sync."""

    executive_id: str
    old_authorization_version: int
    new_authorization_version: int
    products_before: int
    products_after: int


@dataclass(frozen=True, slots=True)
class BrandCatalogSync:
    """Outcome of replacing the stored snapshot with a new catalog read."""

    catalog_digest: str
    source_ref: str
    products: int
    brands: int
    products_without_brand: int
    products_changed: int
    executives_changed: tuple[ExecutiveScopeChange, ...] = field(default=())


@dataclass(frozen=True, slots=True)
class BrandAssignment:
    """An executive's assigned brands and their current access.

    ``brand_products`` counts each assigned brand's products in the synced
    catalog; 0 means the brand currently grants nothing.
    """

    access: ExecutiveAccess
    brand_products: Mapping[str, int]

    @property
    def brands(self) -> frozenset[str]:
        return frozenset(self.brand_products)

    @property
    def unmatched(self) -> frozenset[str]:
        return frozenset(b for b, n in self.brand_products.items() if n == 0)
