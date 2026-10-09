"""Brand-based manager access: assign brands and sync the brand catalog.

Brands are resolved to products only by trusted code: the stored snapshot of
``products.brand`` (``sync_catalog``) joined with exact assigned brand names.
The resolved products become part of ``ExecutiveAccess.product_ids``, which
every existing check already enforces (compiler product binding, result
privacy, evidence scope stamps, report required-scope coverage, release
rechecks and schema-context cache keys). There is no second enforcement path.

Assigned brands never come from model or user text: these methods are for
operator commands (``retail-analytics-dev-access brands ...``) only.
"""

from __future__ import annotations

from collections.abc import Iterable

from retail_analytics.application.contracts.access_audit import SYSTEM_ACTOR
from retail_analytics.application.contracts.brand_access import (
    BrandAssignment,
    BrandCatalogSync,
)
from retail_analytics.application.contracts.persistence import RecordNotFound
from retail_analytics.application.ports.authorization import ExecutiveDirectory
from retail_analytics.application.ports.brand_access import (
    BrandAccessStore,
    ProductBrandSource,
)
from retail_analytics.domain.access import ExecutiveAccess, is_valid_brand


class ProductBrandsUnavailable(Exception):
    """The trusted catalog could not be read; nothing was changed."""


class BrandAccessError(Exception):
    """A refused brand change (``code``: invalid_brand, unknown_brand,
    empty_catalog)."""

    def __init__(self, code: str, brands: Iterable[str] = ()) -> None:
        self.code = code
        self.brands = tuple(sorted(brands))
        detail = f": {', '.join(self.brands)}" if self.brands else ""
        super().__init__(f"{code}{detail}")


def _validated(brands: Iterable[str]) -> frozenset[str]:
    wanted = frozenset(brands)
    invalid = [b for b in wanted if not is_valid_brand(b)]
    if invalid:
        raise BrandAccessError("invalid_brand", (repr(b) for b in invalid))
    return wanted


class BrandAccessService:
    def __init__(
        self,
        store: BrandAccessStore,
        directory: ExecutiveDirectory,
        source: ProductBrandSource | None = None,
    ) -> None:
        self._store = store
        self._directory = directory
        self._source = source

    async def assignment(self, executive_id: str) -> BrandAssignment:
        brands = await self._store.brands_of(executive_id)
        return await self._result(executive_id, brands, None)

    async def replace(
        self,
        executive_id: str,
        brands: Iterable[str],
        *,
        actor_id: str = SYSTEM_ACTOR,
        allow_unmatched: bool = False,
    ) -> BrandAssignment:
        """Set the complete assignment.

        Brands must match the synced catalog exactly (no case folding or fuzzy
        matching). ``allow_unmatched`` stores a brand the snapshot does not
        know yet: it grants nothing until a sync finds products for it.
        """
        wanted = _validated(brands)
        sizes = await self._store.catalog_brand_sizes(wanted)
        unmatched = [b for b in wanted if not sizes.get(b)]
        if unmatched and not allow_unmatched:
            raise BrandAccessError("unknown_brand", unmatched)
        access = await self._store.replace_brands(
            executive_id, wanted, actor_id=actor_id
        )
        return await self._result(executive_id, wanted, access)

    async def assign(
        self,
        executive_id: str,
        brands: Iterable[str],
        *,
        actor_id: str = SYSTEM_ACTOR,
        allow_unmatched: bool = False,
    ) -> BrandAssignment:
        added = _validated(brands)
        current = await self._store.brands_of(executive_id)
        sizes = await self._store.catalog_brand_sizes(added - current)
        unmatched = [b for b in added - current if not sizes.get(b)]
        if unmatched and not allow_unmatched:
            raise BrandAccessError("unknown_brand", unmatched)
        access = await self._store.replace_brands(
            executive_id, current | added, actor_id=actor_id
        )
        return await self._result(executive_id, current | added, access)

    async def remove(
        self,
        executive_id: str,
        brands: Iterable[str],
        *,
        actor_id: str = SYSTEM_ACTOR,
    ) -> BrandAssignment:
        removed = frozenset(brands)
        current = await self._store.brands_of(executive_id)
        access = await self._store.replace_brands(
            executive_id, current - removed, actor_id=actor_id
        )
        return await self._result(executive_id, current - removed, access)

    async def sync_catalog(self, *, actor_id: str = SYSTEM_ACTOR) -> BrandCatalogSync:
        """Re-read ``products.brand`` and replace the stored snapshot.

        A catalog with no branded products is refused (it would revoke every
        brand grant, and usually means the source lacks the brand column);
        the previous snapshot stays in force.
        """
        if self._source is None:
            raise ProductBrandsUnavailable("no product-brand source configured")
        catalog = await self._source.read_product_brands()
        if not catalog.brands:
            raise BrandAccessError("empty_catalog")
        return await self._store.sync_catalog(catalog, actor_id=actor_id)

    async def _result(
        self, executive_id: str, brands: frozenset[str], access: ExecutiveAccess | None
    ) -> BrandAssignment:
        sizes = await self._store.catalog_brand_sizes(brands)
        if access is None:
            access = await self._directory.get(executive_id)
            if access is None:
                raise RecordNotFound("executive", executive_id)
        return BrandAssignment(
            access=access,
            brand_products={b: sizes.get(b, 0) for b in sorted(brands)},
        )
