"""Source currency state.

The dataset's currency is not established by its data: prices must never be used
to infer it. Until verified dataset metadata says otherwise (T34 owns live
verification) the state is UNKNOWN, and no currency symbol may be shown.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SourceCurrency:
    """Currency of source amounts: a verified ISO 4217 code, or unknown."""

    code: str | None = None

    def __post_init__(self) -> None:
        if self.code is not None and not (
            len(self.code) == 3 and self.code.isalpha() and self.code.isupper()
        ):
            raise ValueError("currency code must be three uppercase letters")

    @classmethod
    def unknown(cls) -> SourceCurrency:
        return cls(None)

    @property
    def is_known(self) -> bool:
        return self.code is not None

    def label(self) -> str:
        """Text to show next to an amount; never a guessed symbol."""
        return self.code if self.code is not None else "currency not verified"
