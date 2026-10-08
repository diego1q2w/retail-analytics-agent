"""Source currency state.

The dataset's currency is not established by its data: prices must never be used
to infer it. The state is UNKNOWN unless the operator DECLARES a currency in
configuration. A declared currency is never presented as verified.
"""

from __future__ import annotations

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
