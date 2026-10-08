"""Live smoke of the public Frankfurter/ECB rate API (keyless; three requests).

Skipped when the API cannot be reached, so an offline machine does not fail.
"""

from __future__ import annotations

from datetime import date

import pytest

from retail_analytics.adapters.exchange_rates.frankfurter import FrankfurterRateProvider
from retail_analytics.application.currency_conversion import (
    RateProviderFailure,
    RateUnavailable,
)

pytestmark = pytest.mark.live


@pytest.mark.asyncio
async def test_live_ecb_rates_latest_dated_and_unsupported() -> None:
    provider = FrankfurterRateProvider(timeout=15.0)
    try:
        latest = await provider.rate("USD", "EUR", on=None)
    except RateProviderFailure:
        await provider.aclose()
        pytest.skip("Frankfurter API not reachable")
    try:
        assert 0 < latest.rate < 10
        saturday = date(2026, 10, 3)
        dated = await provider.rate("USD", "EUR", on=saturday)
        assert dated.effective_date <= saturday
        assert dated.effective_date.weekday() < 5
        with pytest.raises(RateUnavailable):
            await provider.rate("USD", "ARS", on=None)
    finally:
        await provider.aclose()
