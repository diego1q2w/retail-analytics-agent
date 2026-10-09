"""Run budgets: how much one investigation may spend, and what it has spent.

A run's limits are pinned when its accounting opens, so a configuration change
or a resumed worker never grants a running investigation a fresh allowance.
Usage only grows: a retried activity, provider fallback or resumption charges
the same counters, and a repeated charge with the same key is the same charge
(see :class:`Charge`). Pure rules here; the store applies them atomically.

Accounting rules:

- Active time is wall-clock time while the run is active. Only waiting for the
  user's clarification pauses it; waiting on the warehouse, backoff and model
  calls all count.
- Queries: every warehouse job submission is one query execution, charged with
  its dry-run estimate before the job exists and settled with the billed bytes
  (processed bytes when billing is not reported) once the job finishes. An
  unsettled charge keeps its estimate.
- Provider requests: every request actually sent (fallback included) is
  charged before it is sent, with the caller's input-token estimate. Settling
  replaces the estimate with reported input+output tokens. When the provider
  reports no usage the estimate stays and the charge is marked ambiguous.
- Model spend: after each provider request settles, its estimated cost (USD,
  stored in micro-dollars) is added to the run; the next request is refused
  once the total reaches the run's limit. This is a soft ceiling: the request
  that crosses the limit, and requests already in flight, are not undone. A
  request whose price is unknown is counted as unpriced and, while a limit
  applies, refuses further requests: unknown is never treated as free.
- Corrections: each reformulation of a failed query is charged to the chain's
  first query; at most ``corrections_per_query`` per chain.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum

GIB = 1024**3


class BudgetResource(StrEnum):
    ACTIVE_TIME = "active_time"
    PROVIDER_REQUESTS = "provider_requests"
    TOKENS = "tokens"
    QUERIES = "queries"
    QUERY_BYTES = "query_bytes"
    RUN_BYTES = "run_bytes"
    CORRECTIONS = "corrections"
    TRANSIENT_ATTEMPTS = "transient_attempts"
    # Estimated model spend reached the run's dollar limit.
    MODEL_COST = "model_cost"
    # A model request had no known price while a dollar limit applies.
    MODEL_PRICE = "model_price"


# Limits a narrower or different request can still satisfy; the others end
# the run's ability to do more of that kind of work.
PER_REQUEST_RESOURCES = frozenset(
    {
        BudgetResource.QUERY_BYTES,
        BudgetResource.CORRECTIONS,
        BudgetResource.TRANSIENT_ATTEMPTS,
    }
)


class BudgetExhausted(Exception):
    """A charge would exceed a limit. Carries no request content."""

    def __init__(self, resource: BudgetResource) -> None:
        self.resource = resource
        super().__init__(f"budget exhausted: {resource.value}")

    @property
    def run_exhausted(self) -> bool:
        """True when the run itself cannot do more of this kind of work."""
        return self.resource not in PER_REQUEST_RESOURCES


@dataclass(frozen=True, slots=True)
class RunLimits:
    """Per-run limits (design defaults); pinned when accounting opens."""

    active_seconds: int = 600
    provider_requests: int = 20
    tokens: int = 100_000
    queries: int = 10
    bytes_per_query: int = GIB
    bytes_per_run: int = 5 * GIB
    corrections_per_query: int = 2
    transient_attempts: int = 3
    query_deadline_seconds: int = 120
    result_rows: int = 500
    result_bytes: int = 256 * 1024
    # Estimated model spend per run in micro-USD; 0 = no dollar limit (runs
    # opened before the limit existed). Configuration sets the default.
    model_cost_micros: int = 0

    def __post_init__(self) -> None:
        for name in type(self).__slots__:
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must not be negative")
        if self.transient_attempts < 1 or self.query_deadline_seconds < 1:
            raise ValueError("at least one attempt and a positive deadline")
        if self.bytes_per_query > self.bytes_per_run:
            raise ValueError("bytes_per_query exceeds bytes_per_run")

    def as_dict(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in type(self).__slots__}

    @classmethod
    def from_dict(cls, values: dict[str, int]) -> RunLimits:
        known = set(cls.__slots__)
        return cls(**{k: int(v) for k, v in values.items() if k in known})


@dataclass(frozen=True, slots=True)
class RunUsage:
    active_seconds_used: float = 0.0
    # Start of the current active period; None while paused for clarification.
    active_since: datetime | None = None
    provider_requests: int = 0
    tokens: int = 0
    queries: int = 0
    bytes: int = 0
    # Estimated spend of priced provider requests (micro-USD).
    model_cost_micros: int = 0
    # Settled provider requests whose cost is unknown (no price).
    unpriced_requests: int = 0

    def active_seconds(self, at: datetime) -> float:
        running = 0.0
        if self.active_since is not None:
            running = max((at - self.active_since).total_seconds(), 0.0)
        return self.active_seconds_used + running


class ChargeKind(StrEnum):
    QUERY = "query"
    PROVIDER_REQUEST = "provider_request"
    CORRECTION = "correction"


MICROS_PER_USD = 1_000_000


@dataclass(frozen=True, slots=True)
class AttemptCost:
    """Estimated spend of one provider request, decided by the caller.

    ``micros`` is None when the price is unknown. ``detail`` is the audit
    record (usage categories, provider, model, price basis); numbers and
    codes only, never request content.
    """

    micros: int | None
    detail: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.micros is not None and self.micros < 0:
            raise ValueError("cost must not be negative")


@dataclass(frozen=True, slots=True)
class Charge:
    """One accounted unit of work, idempotent on (run, kind, key)."""

    kind: ChargeKind
    key: str
    # Correction chain root (the first query of the chain); else None.
    group: str | None = None
    bytes: int = 0
    tokens: int = 0
    settled: bool = False
    # Settled without provider-reported usage: the estimate stands.
    ambiguous: bool = False
    # Provider requests: estimated cost once settled; None while unsettled
    # or when the price is unknown (``detail`` tells which).
    cost_micros: int | None = None
    detail: Mapping[str, object] | None = None


def query_charge_key(operation_id: str, submission: int) -> str:
    return f"{operation_id}#{submission}"


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    """Read-only view of a run's limits and usage at one instant."""

    run_id: str
    limits: RunLimits
    usage: RunUsage
    at: datetime

    @property
    def active_seconds(self) -> float:
        return self.usage.active_seconds(self.at)

    @property
    def paused(self) -> bool:
        return self.usage.active_since is None

    def remaining(self) -> dict[BudgetResource, float]:
        limits, usage = self.limits, self.usage
        return {
            BudgetResource.ACTIVE_TIME: max(
                limits.active_seconds - self.active_seconds, 0.0
            ),
            BudgetResource.PROVIDER_REQUESTS: max(
                limits.provider_requests - usage.provider_requests, 0
            ),
            BudgetResource.TOKENS: max(limits.tokens - usage.tokens, 0),
            BudgetResource.QUERIES: max(limits.queries - usage.queries, 0),
            BudgetResource.RUN_BYTES: max(limits.bytes_per_run - usage.bytes, 0),
            **(
                {
                    BudgetResource.MODEL_COST: max(
                        limits.model_cost_micros - usage.model_cost_micros, 0
                    )
                }
                if limits.model_cost_micros > 0
                else {}
            ),
        }

    def exhausted(self) -> frozenset[BudgetResource]:
        spent = {r for r, left in self.remaining().items() if left <= 0}
        if self.limits.model_cost_micros > 0 and self.usage.unpriced_requests:
            spent.add(BudgetResource.MODEL_PRICE)
        return frozenset(spent)


@dataclass(frozen=True, slots=True)
class RunBudget:
    """The run's accounting aggregate: limits, usage and the rules to charge."""

    run_id: str
    limits: RunLimits
    usage: RunUsage

    @classmethod
    def open(cls, run_id: str, limits: RunLimits, *, at: datetime) -> RunBudget:
        return cls(run_id, limits, RunUsage(active_since=at))

    def snapshot(self, at: datetime) -> BudgetSnapshot:
        return BudgetSnapshot(self.run_id, self.limits, self.usage, at)

    # -- active clock ------------------------------------------------------

    def pause(self, *, at: datetime) -> RunBudget:
        """Stop the active clock (waiting for the user's clarification)."""
        if self.usage.active_since is None:
            return self
        used = self.usage.active_seconds(at)
        return replace(
            self,
            usage=replace(self.usage, active_seconds_used=used, active_since=None),
        )

    def resume(self, *, at: datetime) -> RunBudget:
        if self.usage.active_since is not None:
            return self
        return replace(self, usage=replace(self.usage, active_since=at))

    def _require_time(self, at: datetime) -> None:
        if self.usage.active_seconds(at) >= self.limits.active_seconds:
            raise BudgetExhausted(BudgetResource.ACTIVE_TIME)

    # -- charges -----------------------------------------------------------

    def charge_query(
        self, key: str, estimated_bytes: int, *, at: datetime
    ) -> tuple[RunBudget, Charge]:
        if estimated_bytes < 0:
            raise ValueError("estimated bytes must not be negative")
        self._require_time(at)
        limits, usage = self.limits, self.usage
        if usage.queries >= limits.queries:
            raise BudgetExhausted(BudgetResource.QUERIES)
        if estimated_bytes > limits.bytes_per_query:
            raise BudgetExhausted(BudgetResource.QUERY_BYTES)
        if usage.bytes + estimated_bytes > limits.bytes_per_run:
            raise BudgetExhausted(BudgetResource.RUN_BYTES)
        charge = Charge(ChargeKind.QUERY, key, bytes=estimated_bytes)
        updated = replace(
            usage, queries=usage.queries + 1, bytes=usage.bytes + estimated_bytes
        )
        return replace(self, usage=updated), charge

    def settle_query(
        self, charge: Charge, actual_bytes: int | None
    ) -> tuple[RunBudget, Charge]:
        """Replace the estimate with the actual bytes, once.

        Actual usage is recorded truthfully even above a limit; later charges
        are then refused.
        """
        if charge.kind is not ChargeKind.QUERY:
            raise ValueError("not a query charge")
        if charge.settled:
            return self, charge
        if actual_bytes is None:
            return self, replace(charge, settled=True, ambiguous=True)
        delta = max(actual_bytes, 0) - charge.bytes
        usage = replace(self.usage, bytes=max(self.usage.bytes + delta, 0))
        return replace(self, usage=usage), replace(
            charge, bytes=max(actual_bytes, 0), settled=True
        )

    def charge_provider_request(
        self, key: str, estimated_tokens: int, *, at: datetime
    ) -> tuple[RunBudget, Charge]:
        if estimated_tokens < 0:
            raise ValueError("estimated tokens must not be negative")
        self._require_time(at)
        limits, usage = self.limits, self.usage
        if usage.provider_requests >= limits.provider_requests:
            raise BudgetExhausted(BudgetResource.PROVIDER_REQUESTS)
        if usage.tokens >= limits.tokens or (
            usage.tokens + estimated_tokens > limits.tokens
        ):
            raise BudgetExhausted(BudgetResource.TOKENS)
        if limits.model_cost_micros > 0:
            if usage.unpriced_requests:
                raise BudgetExhausted(BudgetResource.MODEL_PRICE)
            if usage.model_cost_micros >= limits.model_cost_micros:
                raise BudgetExhausted(BudgetResource.MODEL_COST)
        charge = Charge(ChargeKind.PROVIDER_REQUEST, key, tokens=estimated_tokens)
        updated = replace(
            usage,
            provider_requests=usage.provider_requests + 1,
            tokens=usage.tokens + estimated_tokens,
        )
        return replace(self, usage=updated), charge

    def settle_provider_request(
        self,
        charge: Charge,
        reported_tokens: int | None,
        cost: AttemptCost | None = None,
    ) -> tuple[RunBudget, Charge]:
        """Settle once: reported tokens replace the estimate; ``cost`` (when
        given) adds the request's estimated spend, or counts it unpriced."""
        if charge.kind is not ChargeKind.PROVIDER_REQUEST:
            raise ValueError("not a provider request charge")
        if charge.settled:
            return self, charge
        usage = self.usage
        if cost is not None:
            charge = replace(charge, cost_micros=cost.micros, detail=dict(cost.detail))
            if cost.micros is None:
                usage = replace(usage, unpriced_requests=usage.unpriced_requests + 1)
            else:
                usage = replace(
                    usage, model_cost_micros=usage.model_cost_micros + cost.micros
                )
        if reported_tokens is None:
            return replace(self, usage=usage), replace(
                charge, settled=True, ambiguous=True
            )
        delta = max(reported_tokens, 0) - charge.tokens
        usage = replace(usage, tokens=max(usage.tokens + delta, 0))
        return replace(self, usage=usage), replace(
            charge, tokens=max(reported_tokens, 0), settled=True
        )

    def charge_correction(
        self, key: str, group: str, used_in_group: int, *, at: datetime
    ) -> Charge:
        """A reformulation of a failed query; usage is the charge row itself."""
        self._require_time(at)
        if used_in_group >= self.limits.corrections_per_query:
            raise BudgetExhausted(BudgetResource.CORRECTIONS)
        return Charge(ChargeKind.CORRECTION, key, group=group)


def backoff_delay(
    failures: int,
    *,
    base_seconds: float,
    max_seconds: float,
    jitter: float,
    retry_after: float | None = None,
) -> float:
    """Delay before the next attempt after ``failures`` transient failures.

    Exponential with "equal jitter" (half fixed, half random, ``jitter`` in
    [0, 1)) so retries spread out but never fire immediately; a provider's
    retry-after hint is honoured when it is longer.
    """
    if failures < 1:
        raise ValueError("failures starts at 1")
    if not 0.0 <= jitter < 1.0:
        raise ValueError("jitter must be in [0, 1)")
    ceiling = min(max_seconds, base_seconds * 2.0 ** (failures - 1))
    delay = ceiling / 2 + (ceiling / 2) * jitter
    if retry_after is not None and retry_after > delay:
        delay = retry_after
    return delay
