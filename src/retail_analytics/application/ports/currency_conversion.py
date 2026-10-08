from __future__ import annotations

from datetime import date
from typing import Protocol

from retail_analytics.application.contracts.tools import ExecutionContext
from retail_analytics.domain.currency import SourceCurrency
from retail_analytics.domain.exchange_rates import ExchangeRate


class ExchangeRateProvider(Protocol):
    """Source of rates. ``on=None`` asks for the latest published rate."""

    async def rate(self, base: str, quote: str, *, on: date | None) -> ExchangeRate:
        """Raise ``RateUnavailable`` or ``RateProviderFailure``; never guess."""
        ...


class SourceCurrencyProvider(Protocol):
    """Verified currency of the dataset's amounts (T34 owns verification)."""

    async def source_currency(self, ctx: ExecutionContext) -> SourceCurrency: ...


class DisplayCurrencyProvider(Protocol):
    """The executive's saved or session display-currency preference, if any."""

    async def display_currency(self, ctx: ExecutionContext) -> str | None: ...
