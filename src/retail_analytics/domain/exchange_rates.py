"""Exchange rates and the arithmetic of converting an amount with one.

A conversion never replaces a source value: callers keep the original amount
and its source currency and show the rate that was used (value, effective date,
source and method). Rounding happens once, at display precision, and only on
the converted figure.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

_CODE = re.compile(r"[A-Z]{3}")
# ISO 4217 minor units that differ from the usual two decimals (currencies a
# rate provider may offer). Anything not listed uses two.
_MINOR_UNITS = {
    "BIF": 0,
    "CLP": 0,
    "DJF": 0,
    "GNF": 0,
    "ISK": 0,
    "JPY": 0,
    "KMF": 0,
    "KRW": 0,
    "PYG": 0,
    "RWF": 0,
    "UGX": 0,
    "UYI": 0,
    "VND": 0,
    "VUV": 0,
    "XAF": 0,
    "XOF": 0,
    "XPF": 0,
    "BHD": 3,
    "IQD": 3,
    "JOD": 3,
    "KWD": 3,
    "LYD": 3,
    "OMR": 3,
    "TND": 3,
}


class RateBasis(StrEnum):
    """Which rate a conversion uses; the two can differ materially."""

    # The latest published rate, whatever date the amounts relate to.
    CURRENT = "current"
    # The rate in effect on a stated date (for example the period's end).
    HISTORICAL = "historical"


class InvalidRate(ValueError):
    """A rate or currency code is malformed (a provider or caller bug)."""


def check_currency_code(code: str) -> str:
    if not _CODE.fullmatch(code):
        raise InvalidRate("currency code must be three uppercase letters")
    return code


def minor_units(code: str) -> int:
    return _MINOR_UNITS.get(check_currency_code(code), 2)


@dataclass(frozen=True, slots=True)
class ExchangeRate:
    """One quoted rate: 1 unit of ``base`` is ``rate`` units of ``quote``.

    ``effective_date`` is the date the provider says the rate belongs to; it can
    be earlier than ``requested_date`` (weekends, holidays). ``source`` names who
    published it and ``method`` how it was derived, both shown to the user.
    """

    base: str
    quote: str
    rate: Decimal
    effective_date: date
    source: str
    method: str
    requested_date: date | None = None

    def __post_init__(self) -> None:
        check_currency_code(self.base)
        check_currency_code(self.quote)
        if not (self.rate.is_finite() and self.rate > 0):
            raise InvalidRate("rate must be a positive finite number")
        if not self.source.strip() or not self.method.strip():
            raise InvalidRate("a rate needs its source and method")
        if (
            self.requested_date is not None
            and self.effective_date > self.requested_date
        ):
            raise InvalidRate("a rate cannot be effective after the requested date")

    def convert(self, amount: Decimal) -> Decimal:
        """Convert and round to the quote currency's minor units (half up)."""
        step = Decimal(1).scaleb(-minor_units(self.quote))
        return (amount * self.rate).quantize(step, rounding=ROUND_HALF_UP)

    def describe(self) -> str:
        """The disclosure shown next to converted figures."""
        text = (
            f"1 {self.base} = {self.rate} {self.quote} "
            f"({self.source}; {self.method}; rate dated {self.effective_date})"
        )
        if (
            self.requested_date is not None
            and self.requested_date != self.effective_date
        ):
            text += f", latest published on or before {self.requested_date}"
        return text
