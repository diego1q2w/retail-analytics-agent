from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from retail_analytics.application.contracts.budgets import (
    ProviderPermit,
    ProviderUsage,
)
from retail_analytics.domain.budgets import (
    Charge,
    RunBudget,
    RunLimits,
)


class RunBudgetStore(Protocol):
    """Persisted run accounting. Every mutation is atomic per run.

    Charges are idempotent on (run, kind, key): repeating one returns the
    recorded charge without counting again. ``limits`` only applies when the
    run has no accounting yet (it is then opened, clock running); existing
    accounting keeps its pinned limits. Refusals raise ``BudgetExhausted``.
    """

    async def open(
        self, run_id: str, limits: RunLimits, *, at: datetime
    ) -> RunBudget: ...

    async def get(self, run_id: str) -> RunBudget | None: ...

    async def pause(self, run_id: str, *, at: datetime) -> RunBudget:
        """Stop the active clock. Raises ``RecordNotFound`` if never opened."""
        ...

    async def resume(self, run_id: str, *, at: datetime) -> RunBudget: ...

    async def charge_query(
        self,
        run_id: str,
        key: str,
        estimated_bytes: int,
        *,
        limits: RunLimits,
        at: datetime,
    ) -> Charge: ...

    async def settle_query(
        self, run_id: str, key: str, actual_bytes: int | None
    ) -> Charge | None:
        """Settle once; ``None`` when no such charge exists."""
        ...

    async def charge_provider_request(
        self,
        run_id: str,
        key: str,
        estimated_tokens: int,
        *,
        limits: RunLimits,
        at: datetime,
    ) -> Charge: ...

    async def settle_provider_request(
        self, run_id: str, key: str, reported_tokens: int | None
    ) -> Charge | None: ...

    async def charge_correction(
        self,
        run_id: str,
        key: str,
        corrects: str,
        *,
        limits: RunLimits,
        at: datetime,
    ) -> Charge:
        """Charge ``key`` as a reformulation of ``corrects``.

        The chain root is ``corrects`` itself, or the root ``corrects`` was
        charged to if it is a correction too.
        """
        ...

    async def charges(self, run_id: str) -> Sequence[Charge]: ...


class ProviderBudget(Protocol):
    """What a model provider adapter calls around every request it sends.

    ``request_key`` must be stable across retries of the same logical request
    attempt (for example ``<run>/<turn>/<provider>/<attempt>``) and new for
    every request actually sent, fallback included. Reserve before sending
    (raises ``BudgetExhausted``); record after the response or failure. A
    request that failed before reaching the provider may be recorded with
    zero usage.
    """

    async def reserve_provider_request(
        self, run_id: str, request_key: str, *, estimated_input_tokens: int
    ) -> ProviderPermit: ...

    async def record_provider_usage(
        self, run_id: str, request_key: str, usage: ProviderUsage
    ) -> None: ...
