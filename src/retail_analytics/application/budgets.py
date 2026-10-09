"""Run budget enforcement: one shared account per investigation.

``RunBudgets`` is the single gate every metered piece of work passes:

- warehouse queries, as the ``QueryAdmission`` and ``QueryUsageRecorder`` of
  ``QueryExecutionService``;
- model provider requests, through :class:`ProviderBudget` (the runtime's
  provider adapter calls it around every request it sends, fallback included),
  including their estimated dollar cost (``ModelPricing``);
- SQL reformulations (:meth:`RunBudgets.reserve_correction`);
- transient retries (:meth:`RunBudgets.retry_decision`), with backoff;
- the active-time deadline: :func:`within_active_time` stops awaiting
  in-flight model and tool work when the run's active time runs out (checks
  between steps alone would let a long stream or warehouse wait run past it).

Accounting is persisted (``RunBudgetStore``) and every charge is idempotent on
a caller key, so activity retries, worker restarts and resumption charge once
and never reset anything. Budgets reach handlers only as the read-only
``ExecutionContext.budget`` snapshot; they are never tool arguments.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal

from retail_analytics.application.contracts.budgets import (
    ProviderPermit,
    ProviderUsage,
)
from retail_analytics.application.contracts.model_costs import (
    CostEstimate,
    ModelRef,
    PriceBasis,
)
from retail_analytics.application.contracts.persistence import RecordNotFound
from retail_analytics.application.contracts.query_compiler import CompiledQuery
from retail_analytics.application.contracts.tools import ExecutionContext
from retail_analytics.application.contracts.warehouse_jobs import JobStatistics
from retail_analytics.application.ports.budgets import RunBudgetStore
from retail_analytics.application.ports.model_costs import ModelPricing
from retail_analytics.application.query_execution import QueryNotAdmitted
from retail_analytics.domain.budgets import (
    MICROS_PER_USD,
    AttemptCost,
    BudgetExhausted,
    BudgetResource,
    BudgetSnapshot,
    Charge,
    RunLimits,
    backoff_delay,
    query_charge_key,
)
from retail_analytics.domain.operations import ToolErrorCode


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
    BudgetResource.TOKENS: (
        "This investigation has used its model token budget (the next model "
        "request would not fit); no more model requests can be made."
    ),
    BudgetResource.PROVIDER_REQUESTS: (
        "This investigation has used its model request budget; no more model "
        "requests can be made."
    ),
    BudgetResource.CORRECTIONS: (
        "This query has been reformulated the maximum number of times."
    ),
    BudgetResource.TRANSIENT_ATTEMPTS: (
        "The warehouse kept failing; the retry limit for this query is reached."
    ),
    BudgetResource.MODEL_COST: (
        "This investigation has reached its estimated model spending limit; "
        "no more model requests can be made."
    ),
    BudgetResource.MODEL_PRICE: (
        "The model's price is unknown, so its spending cannot be limited; no "
        "more model requests can be made (an operator can set a price)."
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


def usd_to_micros(amount: Decimal) -> int:
    """Whole micro-dollars, rounded up: an estimate never rounds to free."""
    return int((amount * MICROS_PER_USD).to_integral_value(rounding=ROUND_CEILING))


def _count(value: int | None) -> int | None:
    return None if value is None else max(value, 0)


def cost_detail(
    model: ModelRef | None,
    usage: ProviderUsage,
    *,
    estimated_input_tokens: int,
    basis: PriceBasis | None,
    estimate: CostEstimate | None,
) -> dict[str, object]:
    """Audit record of one priced request: usage categories as reported
    (``None`` = not reported), what was priced and on what basis."""
    detail: dict[str, object] = {
        "provider": model.provider if model else None,
        "model": model.model if model else None,
        "usage_reported": usage.reported,
        "input_tokens": _count(usage.input_tokens),
        "cached_input_tokens": _count(usage.cached_input_tokens),
        "cache_write_tokens": _count(usage.cache_write_tokens),
        "output_tokens": _count(usage.output_tokens),
        "reasoning_tokens": _count(usage.reasoning_tokens),
        "price_source": basis.source if basis else None,
        "price_version": basis.version if basis else None,
        "price_override": basis.overridden if basis else False,
    }
    if not usage.reported:
        # No usage: the reservation's input estimate is priced instead
        # (a lower bound: output is unknown).
        detail["estimated_input_tokens"] = estimated_input_tokens
    if estimate is not None:
        detail["input_cost_usd"] = str(estimate.input_usd)
        detail["output_cost_usd"] = str(estimate.output_usd)
    return detail


class ActiveDeadlineReached(BudgetExhausted):
    """The run's active time ran out while work was in flight."""

    def __init__(self) -> None:
        super().__init__(BudgetResource.ACTIVE_TIME)


async def within_active_time[T](seconds_left: float | None, work: Awaitable[T]) -> T:
    """Await ``work`` until the run's active time runs out.

    ``seconds_left`` is what remains of the run's allowance (``None``: no
    limit known, wait normally). At the deadline the awaited work is
    cancelled - a model stream is closed, a warehouse wait or backoff sleep
    ends - and ``ActiveDeadlineReached`` is raised. Remote work the cancelled
    code had started (a warehouse job) is not stopped by this; the caller's
    stop path requests its cancellation by recorded reference.
    """
    if seconds_left is None:
        return await work
    if seconds_left <= 0:
        close = getattr(work, "close", None)
        if callable(close):
            close()
        raise ActiveDeadlineReached
    scope = asyncio.timeout(seconds_left)
    try:
        async with scope:
            return await work
    except TimeoutError:
        if scope.expired():
            raise ActiveDeadlineReached from None
        raise


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
        pricing: ModelPricing | None = None,
    ) -> None:
        """``pricing`` None: every price is unknown (a dollar limit then
        refuses priced providers; local tests use limits without one)."""
        self._store = store
        self._limits = limits
        self._pricing = pricing
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
        self,
        run_id: str,
        request_key: str,
        *,
        estimated_input_tokens: int,
        model: ModelRef | None = None,
    ) -> ProviderPermit:
        """Charge one request before it is sent.

        Under a dollar limit, a model without a known price is refused here,
        before any paid work: its spending could not be limited.
        """
        now = self._clock()
        if model is not None and self._price_basis(model, now) is None:
            budget = await self._store.get(run_id)
            limits = budget.limits if budget is not None else self._limits
            if limits.model_cost_micros > 0:
                raise BudgetExhausted(BudgetResource.MODEL_PRICE)
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
        self,
        run_id: str,
        request_key: str,
        usage: ProviderUsage,
        *,
        model: ModelRef | None = None,
        estimated_input_tokens: int = 0,
    ) -> Charge | None:
        """Settle the request's tokens and estimated cost, once.

        Reported usage is priced as reported. Without usage (a lost response,
        a timeout, a cut-off stream) the reservation's input estimate is
        priced and the charge stays ambiguous. No ``model`` (a caller that
        does not identify it) records no cost.
        """
        cost = None
        if model is not None:
            cost = self._attempt_cost(model, usage, estimated_input_tokens)
        return await self._store.settle_provider_request(
            run_id, request_key, usage.total, cost
        )

    def _price_basis(self, model: ModelRef, at: datetime) -> PriceBasis | None:
        return None if self._pricing is None else self._pricing.basis(model, at=at)

    def _attempt_cost(
        self, model: ModelRef, usage: ProviderUsage, estimated_input_tokens: int
    ) -> AttemptCost:
        now = self._clock()
        priced = (
            usage
            if usage.reported
            else ProviderUsage(input_tokens=max(estimated_input_tokens, 0))
        )
        estimate = (
            None
            if self._pricing is None
            else self._pricing.estimate(model, priced, at=now)
        )
        if estimate is None and not (priced.total or 0):
            # Nothing was used (a definite rejection): free at any price.
            estimate = CostEstimate(
                Decimal(0), Decimal(0), PriceBasis("none", "no usage")
            )
        detail = cost_detail(
            model,
            usage,
            estimated_input_tokens=estimated_input_tokens,
            basis=None if estimate is None else estimate.basis,
            estimate=estimate,
        )
        if estimate is None:
            return AttemptCost(None, detail)
        return AttemptCost(usd_to_micros(estimate.total_usd), detail)

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
