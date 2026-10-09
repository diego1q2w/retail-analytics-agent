"""Model prices from genai-prices, with operator overrides.

`genai-prices <https://github.com/pydantic/genai-prices>`_ is Pydantic's
maintained price list (already pinned as a Pydantic AI dependency). Its
bundled snapshot is used as installed: no network update, so an estimate is
reproducible for a given package version. It applies cached-input prices,
input-length tiers and dated price changes for the request time. (MLflow
estimates from LiteLLM's price table instead; this application computes its
own estimates and sends them to MLflow, so both show the same numbers.)

An override (``MODEL_PRICE_OVERRIDES``) replaces the list for one exact model
name. A model found in neither has an unknown price: never zero. Local
offline models (Pydantic AI ``function``/``test`` models used in fixture mode
and tests) call no provider and cost nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import genai_prices
from genai_prices import Usage, calc_price
from genai_prices.types import ModelPrice

from retail_analytics.application.contracts.budgets import ProviderUsage
from retail_analytics.application.contracts.model_costs import (
    CostEstimate,
    ModelRef,
    PriceBasis,
)

# Pydantic AI ``Model.system`` -> genai-prices provider id.
PROVIDER_IDS: Mapping[str, str] = {
    "google-interactions": "google",
    "google-gla": "google",
    "google-vertex": "google",
    "google": "google",
    "openai": "openai",
}
# Offline models: no provider request, nothing to pay.
LOCAL_SYSTEMS = frozenset({"function", "test"})
SOURCE = "genai-prices"
LOCAL_BASIS = PriceBasis("local", "no provider")


@dataclass(frozen=True, slots=True)
class PriceOverride:
    """USD per million tokens for one model, set by the operator."""

    input_per_mtok: Decimal
    output_per_mtok: Decimal
    # Unset: cached input is charged at the full input price.
    cached_input_per_mtok: Decimal | None = None

    def __post_init__(self) -> None:
        for value in (
            self.input_per_mtok,
            self.output_per_mtok,
            self.cached_input_per_mtok,
        ):
            if value is not None and (not value.is_finite() or value < 0):
                raise ValueError("prices must be finite and not negative")


def _usage(usage: ProviderUsage) -> Usage:
    total_in = max(usage.input_tokens or 0, 0)
    cached = min(max(usage.cached_input_tokens or 0, 0), total_in)
    written = min(max(usage.cache_write_tokens or 0, 0), total_in - cached)
    return Usage(
        input_tokens=total_in,
        cache_read_tokens=cached,
        cache_write_tokens=written,
        output_tokens=max(usage.output_tokens or 0, 0),
    )


class GenaiPricesCatalog:
    """``ModelPricing`` over genai-prices plus overrides."""

    def __init__(self, overrides: Mapping[str, PriceOverride] | None = None) -> None:
        self._overrides = dict(overrides or {})
        self._version = f"{genai_prices.__version__} bundled snapshot"

    def basis(self, model: ModelRef, *, at: datetime) -> PriceBasis | None:
        return self._priced(model, ProviderUsage(0, 0), at)[0]

    def estimate(
        self, model: ModelRef, usage: ProviderUsage, *, at: datetime
    ) -> CostEstimate | None:
        basis, prices = self._priced(model, usage, at)
        if basis is None or prices is None:
            return None
        return CostEstimate(prices[0], prices[1], basis)

    def _priced(
        self, model: ModelRef, usage: ProviderUsage, at: datetime
    ) -> tuple[PriceBasis | None, tuple[Decimal, Decimal] | None]:
        if model.provider in LOCAL_SYSTEMS:
            return LOCAL_BASIS, (Decimal(0), Decimal(0))
        override = self._overrides.get(model.model)
        if override is not None:
            price = ModelPrice(
                input_mtok=override.input_per_mtok,
                cache_read_mtok=(
                    override.cached_input_per_mtok
                    if override.cached_input_per_mtok is not None
                    else override.input_per_mtok
                ),
                output_mtok=override.output_per_mtok,
            )
            parts = price.calc_price(_usage(usage))
            return (
                PriceBasis("override", "MODEL_PRICE_OVERRIDES", overridden=True),
                (parts["input_price"], parts["output_price"]),
            )
        provider_id = PROVIDER_IDS.get(model.provider)
        if provider_id is None:
            return None, None
        try:
            result = calc_price(
                _usage(usage),
                model.model,
                provider_id=provider_id,
                genai_request_timestamp=at,
            )
        except LookupError:
            return None, None
        return (
            PriceBasis(SOURCE, f"{self._version}, {provider_id}/{result.model.id}"),
            (result.input_price, result.output_price),
        )
