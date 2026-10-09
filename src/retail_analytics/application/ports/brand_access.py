from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Protocol

from retail_analytics.application.contracts.access_audit import SYSTEM_ACTOR
from retail_analytics.application.contracts.brand_access import (
    BrandCatalogSync,
    ProductBrandCatalog,
)
from retail_analytics.domain.access import ExecutiveAccess


class ProductBrandSource(Protocol):
    """Reads ``products.brand`` from the trusted catalog (warehouse).

    Raises ``ProductBrandsUnavailable`` when the catalog cannot be read.
    """

    async def read_product_brands(self) -> ProductBrandCatalog: ...


class BrandAccessStore(Protocol):
    """Brand assignments and the synced product-brand snapshot.

    For trusted operator paths only (never a model tool). Like
    ``AccessAdministration``, every change to what an executive can see
    increments their authorization version and writes one ``access.*`` audit
    event in the same transaction; identical repeats change nothing.
    """

    async def brands_of(self, executive_id: str) -> frozenset[str]:
        """Assigned brands; raises ``RecordNotFound`` for unknown executives."""
        ...

    async def replace_brands(
        self,
        executive_id: str,
        brands: Iterable[str],
        *,
        actor_id: str = SYSTEM_ACTOR,
    ) -> ExecutiveAccess:
        """Make ``brands`` the executive's complete brand assignment."""
        ...

    async def catalog_brand_sizes(self, brands: Iterable[str]) -> Mapping[str, int]:
        """Products per brand in the stored snapshot (0 when unknown)."""
        ...

    async def brands_within(self, product_ids: Iterable[str]) -> frozenset[str]:
        """Distinct snapshot brands of these products (empty when none known)."""
        ...

    async def sync_catalog(
        self, catalog: ProductBrandCatalog, *, actor_id: str = SYSTEM_ACTOR
    ) -> BrandCatalogSync:
        """Replace the stored snapshot and re-version affected executives."""
        ...
