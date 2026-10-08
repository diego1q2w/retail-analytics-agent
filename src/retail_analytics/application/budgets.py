"""Run budget enforcement: one shared account per investigation.

``RunBudgets`` is the single gate every metered piece of work passes:

- warehouse queries, as the ``QueryAdmission`` and ``QueryUsageRecorder`` of
  ``QueryExecutionService``;
- model provider requests, through :class:`ProviderBudget` (the runtime's
  provider adapter calls it around every request it sends, fallback included);
- SQL reformulations (:meth:`RunBudgets.reserve_correction`);
- transient retries (:meth:`RunBudgets.retry_decision`), with backoff.

Accounting is persisted (``RunBudgetStore``) and every charge is idempotent on
a caller key, so activity retries, worker restarts and resumption charge once
and never reset anything. Budgets reach handlers only as the read-only
``ExecutionContext.budget`` snapshot; they are never tool arguments.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Protocol

from retail_analytics.application.persistence import RecordNotFound
from retail_analytics.application.query_compiler import CompiledQuery
from retail_analytics.application.query_execution import QueryNotAdmitted
from retail_analytics.application.tools.context import ExecutionContext
from retail_analytics.application.warehouse_jobs import JobStatistics
from retail_analytics.domain.budgets import (
    BudgetExhausted,
    BudgetResource,
    BudgetSnapshot,
    Charge,
    RunBudget,
    RunLimits,
    backoff_delay,
    query_charge_key,
)
from retail_analytics.domain.operations import ToolErrorCode


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


@dataclass(frozen=True, slots=True)
class RetrySettings:
    base_seconds: float = 1.0
    max_seconds: float = 20.0

    def __post_init__(self) -> None:
        if self.base_seconds <= 0 or self.max_seconds < self.base_seconds:
            raise ValueError("invalid retry settings")


@dataclass(frozen=True, slots=True)
class RetryDecision:
    allowed: bool
    delay_seconds: float = 0.0
    # Why the retry is refused (attempts or active time); None when allowed.
    exhausted: BudgetResource | None = None


_NOT_ADMITTED = {
    BudgetResource.QUERY_BYTES: (
        "The query would read more data than one query may; narrow the period, "
        "filters or columns."
    ),
    BudgetResource.RUN_BYTES: (
        "This investigation has used its data-scan budget; no more queries can run."
    ),
    BudgetResource.QUERIES: (
        "This investigation has used its query budget; no more queries can run."
    ),
    BudgetResource.ACTIVE_TIME: (
        "This investigation has used its time budget; no more work can start."
    ),
    BudgetResource.CORRECTIONS: (
        "This query has been reformulated the maximum number of times."
    ),
    BudgetResource.TRANSIENT_ATTEMPTS: (
        "The warehouse kept failing; the retry limit for this query is reached."
    ),
}


def budget_message(resource: BudgetResource) -> str:
    """User-safe explanation of an exhausted limit."""
    return _NOT_ADMITTED.get(
        resource, "This investigation has used its budget for this kind of work."
    )


def budget_reason(error: BudgetExhausted) -> str:
    prefix = "run_budget" if error.run_exhausted else "query_budget"
    return f"{prefix}_{error.resource.value}"


def _utc_now() -> datetime:
    return datetime.now(UTC)


class RunBudgets:
    """Application gate over the persisted run accounting."""

    def __init__(
        self,
        store: RunBudgetStore,
        limits: RunLimits,
        *,
        retry: RetrySettings | None = None,
        clock: Callable[[], datetime] = _utc_now,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._store = store
        self._limits = limits
        self._retry = retry or RetrySettings()
        self._clock = clock
        self._jitter = jitter

    @property
    def default_limits(self) -> RunLimits:
        return self._limits

    # -- lifecycle ----------------------------------------------------------

    async def open(self, run_id: str) -> BudgetSnapshot:
        """Open the run's accounting (idempotent; never resets usage)."""
        now = self._clock()
        budget = await self._store.open(run_id, self._limits, at=now)
        return budget.snapshot(now)

    async def snapshot(self, run_id: str) -> BudgetSnapshot | None:
        budget = await self._store.get(run_id)
        return None if budget is None else budget.snapshot(self._clock())

    async def with_budget(self, context: ExecutionContext) -> ExecutionContext:
        """``context`` carrying the run's current budget snapshot."""
        snapshot = await self.snapshot(context.correlation.run_id)
        return replace(context, budget=snapshot)

    async def pause_for_clarification(self, run_id: str) -> BudgetSnapshot:
        """Waiting for the user's answer is the only time that is not charged."""
        now = self._clock()
        return (await self._store.pause(run_id, at=now)).snapshot(now)

    async def resume_after_clarification(self, run_id: str) -> BudgetSnapshot:
        now = self._clock()
        return (await self._store.resume(run_id, at=now)).snapshot(now)

    # -- warehouse queries (QueryAdmission + QueryUsageRecorder) ------------

    async def admit(
        self,
        context: ExecutionContext,
        operation_id: str,
        submission: int,
        compiled: CompiledQuery,
        estimated_bytes: int,
    ) -> None:
        run_id = context.correlation.run_id
        try:
            await self._store.charge_query(
                run_id,
                query_charge_key(operation_id, submission),
                estimated_bytes,
                limits=self._limits,
                at=self._clock(),
            )
        except BudgetExhausted as error:
            raise QueryNotAdmitted(
                ToolErrorCode.BUDGET_EXCEEDED,
                budget_reason(error),
                budget_message(error.resource),
            ) from None

    async def settle(
        self,
        run_id: str,
        operation_id: str,
        submission: int,
        statistics: JobStatistics,
    ) -> None:
        actual = statistics.bytes_billed
        if actual is None:
            actual = statistics.bytes_processed
        await self._store.settle_query(
            run_id, query_charge_key(operation_id, submission), actual
        )

    # -- model provider requests (ProviderBudget) ---------------------------

    async def reserve_provider_request(
        self, run_id: str, request_key: str, *, estimated_input_tokens: int
    ) -> ProviderPermit:
        now = self._clock()
        charge = await self._store.charge_provider_request(
            run_id,
            request_key,
            max(estimated_input_tokens, 0),
            limits=self._limits,
            at=now,
        )
        budget = await self._store.get(run_id)
        if budget is None:  # the charge opened it; only a concurrent purge
            raise RecordNotFound("run budget", run_id)
        left = budget.snapshot(now).remaining()
        return ProviderPermit(
            request_key=request_key,
            charged_tokens=charge.tokens,
            remaining_tokens=int(left[BudgetResource.TOKENS]),
            remaining_requests=int(left[BudgetResource.PROVIDER_REQUESTS]),
        )

    async def record_provider_usage(
        self, run_id: str, request_key: str, usage: ProviderUsage
    ) -> None:
        await self._store.settle_provider_request(run_id, request_key, usage.total)

    # -- reformulation and retry --------------------------------------------

    async def reserve_correction(
        self, run_id: str, operation_id: str, *, corrects: str
    ) -> None:
        """Charge a reformulated query operation before it runs.

        Raises ``BudgetExhausted`` (CORRECTIONS, or ACTIVE_TIME) when the
        chain has used its reformulations.
        """
        await self._store.charge_correction(
            run_id, operation_id, corrects, limits=self._limits, at=self._clock()
        )

    async def retry_decision(
        self,
        run_id: str,
        failures: int,
        *,
        retry_after: float | None = None,
    ) -> RetryDecision:
        """Whether a transient failure may be retried, and after what delay.

        ``failures`` counts the transient failures of the operation so far
        (the persisted operation history is the source; see
        ``transient_failures``). Uses the run's pinned limits.
        """
        now = self._clock()
        budget = await self._store.get(run_id)
        if budget is None:
            budget = await self._store.open(run_id, self._limits, at=now)
        limits = budget.limits
        if failures >= limits.transient_attempts:
            return RetryDecision(False, exhausted=BudgetResource.TRANSIENT_ATTEMPTS)
        delay = backoff_delay(
            failures,
            base_seconds=self._retry.base_seconds,
            max_seconds=self._retry.max_seconds,
            jitter=min(max(self._jitter(), 0.0), 0.999999),
            retry_after=retry_after,
        )
        left = limits.active_seconds - budget.usage.active_seconds(now)
        if left <= delay:
            return RetryDecision(False, exhausted=BudgetResource.ACTIVE_TIME)
        return RetryDecision(True, delay_seconds=delay)
