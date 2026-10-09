from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol


class PermittedBrands(Protocol):
    """Brands of given products, from the trusted synced brand snapshot."""

    async def brands_within(self, product_ids: Iterable[str]) -> frozenset[str]:
        """Distinct brands of these products (empty when none are known)."""
        ...
