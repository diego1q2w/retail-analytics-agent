from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    """Token usage a provider reported for one request; ``None`` if absent.

    Normalized per API by the provider adapter: ``input_tokens`` is the whole
    prompt (cached part included), ``output_tokens`` everything billed as
    output (reasoning included). ``cached_input_tokens``,
    ``cache_write_tokens`` and ``reasoning_tokens`` are the subsets the
    provider reported, for pricing and audit; never added on top.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None

    @property
    def reported(self) -> bool:
        return self.input_tokens is not None or self.output_tokens is not None

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
