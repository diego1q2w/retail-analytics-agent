"""Rate arithmetic, minor-unit rounding and the fixture provider's date rules."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from retail_analytics.adapters.exchange_rates.fixture import FixtureRateProvider
from retail_analytics.application.currency_conversion import RateUnavailable
from retail_analytics.domain.exchange_rates import (
    ExchangeRate,
    InvalidRate,
    minor_units,
)


def rate(value: str, quote: str = "EUR", **kw: object) -> ExchangeRate:
    params: dict[str, object] = {
        "base": "USD",
        "quote": quote,
        "rate": Decimal(value),
        "effective_date": date(2026, 10, 2),
        "source": "test",
        "method": "fixed",
    }
    params.update(kw)
    return ExchangeRate(**params)  # type: ignore[arg-type]


def test_rounds_half_up_to_the_quote_currency_minor_units() -> None:
    assert rate("0.845").convert(Decimal(5)) == Decimal("4.23")
    assert rate("0.845").convert(Decimal("-5")) == Decimal("-4.23")
    assert rate("151.5", "JPY").convert(Decimal("10.5")) == Decimal(1591)
    assert rate("0.3", "KWD").convert(Decimal("1.2345")) == Decimal("0.370")
    assert (minor_units("EUR"), minor_units("JPY"), minor_units("KWD")) == (2, 0, 3)


def test_rejects_malformed_rates_and_codes() -> None:
    for bad in ("0", "-1", "NaN", "Infinity"):
        with pytest.raises(InvalidRate):
            rate(bad)
    with pytest.raises(InvalidRate):
        rate("1", base="usd")
    with pytest.raises(InvalidRate):
        rate("1", source=" ")
    with pytest.raises(InvalidRate):
        rate("1", requested_date=date(2026, 10, 1))


def test_description_discloses_value_date_source_and_method() -> None:
    text = rate("0.89", requested_date=date(2026, 10, 4)).describe()
    assert "1 USD = 0.89 EUR" in text
    assert "2026-10-02" in text and "test" in text and "fixed" in text
    assert "on or before 2026-10-04" in text
    assert "on or before" not in rate("0.89").describe()


RATES = {
    ("USD", "EUR"): {
        date(2026, 10, 1): Decimal("0.90"),
        date(2026, 10, 2): Decimal("0.89"),
        date(2026, 10, 8): Decimal("0.88"),
    }
}


@pytest.mark.asyncio
async def test_fixture_provider_picks_latest_published_on_or_before() -> None:
    provider = FixtureRateProvider(RATES)
    weekend = await provider.rate("USD", "EUR", on=date(2026, 10, 4))
    assert (weekend.rate, weekend.effective_date) == (
        Decimal("0.89"),
        date(2026, 10, 2),
    )
    assert weekend.requested_date == date(2026, 10, 4)
    latest = await provider.rate("USD", "EUR", on=None)
    assert latest.effective_date == date(2026, 10, 8)


@pytest.mark.asyncio
async def test_fixture_provider_never_inverts_or_extrapolates() -> None:
    provider = FixtureRateProvider(RATES)
    with pytest.raises(RateUnavailable):
        await provider.rate("EUR", "USD", on=None)
    with pytest.raises(RateUnavailable):
        await provider.rate("USD", "EUR", on=date(2026, 9, 30))
