from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    """Token usage a provider reported for one request; ``None`` if absent."""

    input_tokens: int | None = None
    output_tokens: int | None = None

    @property
    def total(self) -> int | None:
        if self.input_tokens is None and self.output_tokens is None:
            return None
        return (self.input_tokens or 0) + (self.output_tokens or 0)


@dataclass(frozen=True, slots=True)
class ProviderPermit:
    request_key: str
    charged_tokens: int
    remaining_tokens: int
    remaining_requests: int
