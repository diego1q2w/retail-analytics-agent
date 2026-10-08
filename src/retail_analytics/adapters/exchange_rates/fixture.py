"""Deterministic offline rate provider for tests and keyless local runs.

Rates are published on explicit dates, like a central bank's business days. A
dated request gets the latest rate published on or before that date (a weekend
resolves to Friday), and ``on=None`` gets the latest one. A pair with no
published rate is unavailable: nothing is inverted or interpolated.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal

from retail_analytics.application.currency_conversion import RateUnavailable
from retail_analytics.domain.exchange_rates import ExchangeRate

FIXTURE_SOURCE = "fixture rates"
FIXTURE_METHOD = "reference rate fixed for tests"


class FixtureRateProvider:
    def __init__(
        self,
        rates: Mapping[tuple[str, str], Mapping[date, Decimal]],
        *,
        source: str = FIXTURE_SOURCE,
        method: str = FIXTURE_METHOD,
    ) -> None:
        self._rates = {pair: dict(by_date) for pair, by_date in rates.items()}
        self._source = source
        self._method = method
        self.calls: list[tuple[str, str, date | None]] = []
        # Fault injection: raised by the next call(s), then cleared.
        self.fail_with: Exception | None = None

    async def rate(self, base: str, quote: str, *, on: date | None) -> ExchangeRate:
        self.calls.append((base, quote, on))
        if self.fail_with is not None:
            raise self.fail_with
        published = self._rates.get((base, quote))
        if not published:
            raise RateUnavailable(f"no {base}/{quote} rate")
        eligible = [d for d in published if on is None or d <= on]
        if not eligible:
            raise RateUnavailable(f"no {base}/{quote} rate on or before {on}")
        effective = max(eligible)
        return ExchangeRate(
            base,
            quote,
            published[effective],
            effective,
            self._source,
            self._method,
            requested_date=on,
        )
