from __future__ import annotations

from datetime import datetime
from typing import Protocol

from retail_analytics.application.contracts.budgets import ProviderUsage
from retail_analytics.application.contracts.model_costs import (
    CostEstimate,
    ModelRef,
    PriceBasis,
)


class ModelPricing(Protocol):
    """Prices of model requests. ``None`` means the price is unknown.

    ``at`` is when the request was sent (prices can change on a date).
    """

    def basis(self, model: ModelRef, *, at: datetime) -> PriceBasis | None: ...

    def estimate(
        self, model: ModelRef, usage: ProviderUsage, *, at: datetime
    ) -> CostEstimate | None: ...
