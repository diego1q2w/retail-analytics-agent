"""Model price lookups and per-request cost estimates (USD).

Estimates come from a maintained public price list or an operator override;
they are not provider invoices. Amounts are ``Decimal`` USD.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class ModelRef:
    """The provider system and exact model name a request is sent to."""

    provider: str
    model: str


@dataclass(frozen=True, slots=True)
class PriceBasis:
    """Where a price came from: source and version, or an override."""

    source: str
    version: str
    overridden: bool = False


@dataclass(frozen=True, slots=True)
class CostEstimate:
    input_usd: Decimal
    output_usd: Decimal
    basis: PriceBasis

    @property
    def total_usd(self) -> Decimal:
        return self.input_usd + self.output_usd
