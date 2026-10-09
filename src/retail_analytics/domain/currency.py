"""Source currency state.

The dataset's currency is not established by its data: prices must never be used
to infer it. The state is UNKNOWN unless the operator DECLARES a currency in
configuration. A declared currency is never presented as verified.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable
from dataclasses import dataclass

DECLARED_STATEMENT = "source currency declared by operator, not verified from data"


@dataclass(frozen=True, slots=True)
class SourceCurrency:
    """Currency of source amounts: an ISO 4217 code (declared), or unknown."""

    code: str | None = None
    declared: bool = False

    def __post_init__(self) -> None:
        if self.code is not None and not (
            len(self.code) == 3 and self.code.isalpha() and self.code.isupper()
        ):
            raise ValueError("currency code must be three uppercase letters")
        if self.declared and self.code is None:
            raise ValueError("an unknown currency cannot be declared")

    @classmethod
    def declared_by_operator(cls, code: str) -> SourceCurrency:
        return cls(code, declared=True)

    @classmethod
    def unknown(cls) -> SourceCurrency:
        return cls(None)

    @property
    def is_known(self) -> bool:
        return self.code is not None

    def label(self) -> str:
        """Text to show next to an amount; never a guessed symbol."""
        if self.code is None:
            return "currency not verified"
        return f"{self.code} ({DECLARED_STATEMENT})" if self.declared else self.code


DECLARED_QUALIFIER = "declared by the operator, not independently verified"
CURRENCY_SYMBOLS = "$€£¥"
_COMMON_CODES = (
    "USD|EUR|GBP|JPY|CNY|CAD|AUD|NZD|CHF|SEK|NOK|DKK|PLN|CZK|HUF|RON|"
    "BRL|MXN|INR|KRW|SGD|HKD|TRY|ZAR|ILS|AED|SAR|THB|IDR|MYR|PHP"
)
_CODE_BESIDE_AMOUNT = re.compile(
    rf"(?<![A-Za-z])(?:({_COMMON_CODES})(?:\s?\([^)]*\))?\s?[0-9]|[0-9]\s?({_COMMON_CODES})(?![A-Za-z]))"
)


def unsupported_currency_marks(
    texts: Iterable[str],
    converted_codes: Collection[str],
    declared_code: str | None = None,
) -> list[str]:
    """Currency marks the evidence and configuration do not support.

    A symbol is ambiguous (``$`` is many currencies) and is never supported. A
    code beside an amount is supported when a cited conversion produced it, or
    when it is exactly the operator-declared source currency; the declared
    code must then be qualified in the text as declared, not verified.
    """
    texts = tuple(texts)
    found: list[str] = []
    used: set[str] = set()
    for text in texts:
        found += [c for c in text if c in CURRENCY_SYMBOLS]
        for match in _CODE_BESIDE_AMOUNT.finditer(text):
            used.add(match.group(1) or match.group(2))
    for code in sorted(used):
        if code in converted_codes:
            continue
        if code == declared_code:
            if not any(DECLARED_QUALIFIER in t.lower() for t in texts):
                found.append(f"{code} without the qualification '{DECLARED_QUALIFIER}'")
            continue
        found.append(code)
    return sorted(set(found))
