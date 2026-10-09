"""Behaviour every ``RunBudgetStore`` must show, run against each store.

The in-memory store runs these in the unit suite; the PostgreSQL store runs
them in tests/integration/test_budgets.py against a real database, where the
concurrent cases go through separate connections (and processes' pools).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta

import pytest

from retail_analytics.application.ports.budgets import RunBudgetStore
from retail_analytics.domain.budgets import (
    GIB,
    AttemptCost,
    BudgetExhausted,
    BudgetResource,
    ChargeKind,
    RunLimits,
)

T0 = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
LIMITS = RunLimits()


def new_run() -> str:
    return f"run-{uuid.uuid4().hex[:12]}"


async def _outcomes(
    calls: Sequence[Awaitable[object]],
) -> tuple[int, list[BudgetResource]]:
    results = await asyncio.gather(*calls, return_exceptions=True)
    refused: list[BudgetResource] = []
    for result in results:
        if isinstance(result, BudgetExhausted):
            refused.append(result.resource)
        elif isinstance(result, BaseException):
            raise result
    return len(results) - len(refused), refused


async def limits_are_pinned_and_reopening_never_resets(
    stores: Sequence[RunBudgetStore],
) -> None:
    store = stores[0]
    run = new_run()
    await store.open(run, LIMITS, at=T0)
    await store.charge_query(run, "op-1#1", GIB, limits=LIMITS, at=T0)
    reopened = await store.open(run, RunLimits(queries=50), at=T0 + timedelta(1))
    assert reopened.limits == LIMITS
    assert reopened.usage.queries == 1 and reopened.usage.bytes == GIB
    other = stores[-1]
    charge = await other.charge_query(
        run, "op-1#1", 5, limits=RunLimits(queries=50), at=T0
    )
    assert charge.bytes == GIB  # the recorded charge, not a new one
    budget = await other.get(run)
    assert budget is not None and budget.usage.queries == 1


async def concurrent_query_charges_never_exceed_the_query_limit(
    stores: Sequence[RunBudgetStore],
) -> None:
    run = new_run()
    await stores[0].open(run, LIMITS, at=T0)
    calls = [
        stores[i % len(stores)].charge_query(
            run, f"op-{i}#1", 1024, limits=LIMITS, at=T0
        )
        for i in range(25)
    ]
    accepted, refused = await _outcomes(calls)
    assert accepted == 10
    assert set(refused) == {BudgetResource.QUERIES}
    budget = await stores[0].get(run)
    assert budget is not None and budget.usage.queries == 10
    charges = await stores[0].charges(run)
    assert len([c for c in charges if c.kind is ChargeKind.QUERY]) == 10


async def concurrent_retries_of_one_charge_count_once(
    stores: Sequence[RunBudgetStore],
) -> None:
    run = new_run()
    calls = [
        stores[i % len(stores)].charge_query(run, "op-1#1", 4096, limits=LIMITS, at=T0)
        for i in range(20)
    ]
    accepted, refused = await _outcomes(calls)
    assert (accepted, refused) == (20, [])
    budget = await stores[0].get(run)
    assert budget is not None
    assert (budget.usage.queries, budget.usage.bytes) == (1, 4096)


async def concurrent_scans_never_exceed_the_run_byte_limit(
    stores: Sequence[RunBudgetStore],
) -> None:
    run = new_run()
    with pytest.raises(BudgetExhausted) as error:
        await stores[0].charge_query(run, "big#1", GIB + 1, limits=LIMITS, at=T0)
    assert error.value.resource is BudgetResource.QUERY_BYTES
    assert not error.value.run_exhausted
    calls = [
        stores[i % len(stores)].charge_query(
            run, f"op-{i}#1", GIB, limits=LIMITS, at=T0
        )
        for i in range(8)
    ]
    accepted, refused = await _outcomes(calls)
    assert accepted == 5
    assert set(refused) == {BudgetResource.RUN_BYTES}
    budget = await stores[0].get(run)
    assert budget is not None and budget.usage.bytes == 5 * GIB
    assert budget.usage.queries == 5


async def settling_replaces_the_estimate_once(
    stores: Sequence[RunBudgetStore],
) -> None:
    store = stores[0]
    run = new_run()
    await store.charge_query(run, "op-1#1", 1000, limits=LIMITS, at=T0)
    await store.charge_query(run, "op-2#1", 1000, limits=LIMITS, at=T0)
    settled = await store.settle_query(run, "op-1#1", 10 * 1024 * 1024)
    assert settled is not None and settled.settled and settled.bytes == 10485760
    again = await store.settle_query(run, "op-1#1", 1)
    assert again is not None and again.bytes == 10485760
    unknown = await store.settle_query(run, "op-2#1", None)
    assert unknown is not None and unknown.ambiguous and unknown.bytes == 1000
    assert await store.settle_query(run, "op-9#1", 5) is None
    budget = await store.get(run)
    assert budget is not None and budget.usage.bytes == 10485760 + 1000


async def provider_requests_and_tokens_are_capped(
    stores: Sequence[RunBudgetStore],
) -> None:
    run = new_run()
    calls = [
        stores[i % len(stores)].charge_provider_request(
            run, f"req-{i}", 100, limits=LIMITS, at=T0
        )
        for i in range(30)
    ]
    accepted, refused = await _outcomes(calls)
    assert accepted == 20
    assert set(refused) == {BudgetResource.PROVIDER_REQUESTS}

    run = new_run()
    store = stores[0]
    await store.charge_provider_request(run, "a", 1000, limits=LIMITS, at=T0)
    reported = await store.settle_provider_request(run, "a", 99_000)
    assert reported is not None and reported.tokens == 99_000
    with pytest.raises(BudgetExhausted) as error:
        await store.charge_provider_request(run, "b", 1001, limits=LIMITS, at=T0)
    assert error.value.resource is BudgetResource.TOKENS
    await store.charge_provider_request(run, "c", 1000, limits=LIMITS, at=T0)
    ambiguous = await store.settle_provider_request(run, "c", None)
    assert ambiguous is not None and ambiguous.ambiguous and ambiguous.tokens == 1000
    with pytest.raises(BudgetExhausted) as error:
        await store.charge_provider_request(run, "d", 0, limits=LIMITS, at=T0)
    assert error.value.resource is BudgetResource.TOKENS
    budget = await store.get(run)
    assert budget is not None
    assert (budget.usage.provider_requests, budget.usage.tokens) == (2, 100_000)


async def corrections_are_capped_per_chain(
    stores: Sequence[RunBudgetStore],
) -> None:
    store = stores[0]
    run = new_run()
    first = await store.charge_correction(run, "op-2", "op-1", limits=LIMITS, at=T0)
    assert first.group == "op-1"
    second = await store.charge_correction(run, "op-3", "op-2", limits=LIMITS, at=T0)
    assert second.group == "op-1"
    with pytest.raises(BudgetExhausted) as error:
        await store.charge_correction(run, "op-4", "op-3", limits=LIMITS, at=T0)
    assert error.value.resource is BudgetResource.CORRECTIONS
    # A retried reservation of an accepted correction is still accepted.
    again = await store.charge_correction(run, "op-3", "op-2", limits=LIMITS, at=T0)
    assert again == second
    # Another failed query has its own chain.
    await store.charge_correction(run, "op-6", "op-5", limits=LIMITS, at=T0)

    run = new_run()
    calls = [
        stores[i % len(stores)].charge_correction(
            run, f"fix-{i}", "root", limits=LIMITS, at=T0
        )
        for i in range(6)
    ]
    accepted, refused = await _outcomes(calls)
    assert accepted == 2
    assert set(refused) == {BudgetResource.CORRECTIONS}


async def clarification_time_is_excluded_from_active_time(
    stores: Sequence[RunBudgetStore],
) -> None:
    store = stores[0]
    run = new_run()
    await store.open(run, LIMITS, at=T0)
    paused = await store.pause(run, at=T0 + timedelta(seconds=300))
    assert paused.usage.active_since is None
    assert paused.usage.active_seconds(T0 + timedelta(hours=5)) == 300
    # Charges while waiting see only the active time used.
    await store.charge_query(
        run, "op-1#1", 1, limits=LIMITS, at=T0 + timedelta(hours=1)
    )
    resumed = await store.resume(run, at=T0 + timedelta(hours=2))
    at_limit = T0 + timedelta(hours=2, seconds=300)
    assert resumed.usage.active_seconds(at_limit) == 600
    with pytest.raises(BudgetExhausted) as error:
        await stores[-1].charge_query(run, "op-2#1", 1, limits=LIMITS, at=at_limit)
    assert error.value.resource is BudgetResource.ACTIVE_TIME
    assert error.value.run_exhausted
    just_before = at_limit - timedelta(seconds=1)
    await store.charge_provider_request(run, "r", 1, limits=LIMITS, at=just_before)


COST_LIMITS = RunLimits(model_cost_micros=1000)
DETAIL = {"provider": "openai", "model": "gpt-5-mini", "input_tokens": 10}


async def model_spend_settles_once_and_stops_the_next_request(
    stores: Sequence[RunBudgetStore],
) -> None:
    store, other = stores[0], stores[-1]
    run = new_run()
    await store.charge_provider_request(run, "a", 10, limits=COST_LIMITS, at=T0)
    settled = await store.settle_provider_request(
        run, "a", 20, AttemptCost(600, DETAIL)
    )
    assert settled is not None and settled.cost_micros == 600
    assert settled.detail == DETAIL
    # A replayed settlement (another worker, a retried activity) is the same one.
    again = await other.settle_provider_request(run, "a", 20, AttemptCost(600, DETAIL))
    assert again is not None and again.cost_micros == 600
    await other.charge_provider_request(run, "b", 10, limits=COST_LIMITS, at=T0)
    await other.settle_provider_request(run, "b", 20, AttemptCost(500, DETAIL))
    # 1100 > 1000: the soft limit let "b" finish and refuses what comes next.
    with pytest.raises(BudgetExhausted) as error:
        await store.charge_provider_request(run, "c", 10, limits=COST_LIMITS, at=T0)
    assert error.value.resource is BudgetResource.MODEL_COST
    assert error.value.run_exhausted
    reopened = await other.open(run, RunLimits(model_cost_micros=10**9), at=T0)
    assert reopened.limits == COST_LIMITS  # never a fresh allowance
    assert reopened.usage.model_cost_micros == 1100
    assert reopened.snapshot(T0).exhausted() >= {BudgetResource.MODEL_COST}
    costs = {
        c.key: c.cost_micros
        for c in await store.charges(run)
        if c.kind is ChargeKind.PROVIDER_REQUEST
    }
    assert costs == {"a": 600, "b": 500}


async def unknown_prices_are_never_free(
    stores: Sequence[RunBudgetStore],
) -> None:
    store = stores[0]
    run = new_run()
    await store.charge_provider_request(run, "a", 10, limits=COST_LIMITS, at=T0)
    unknown = await store.settle_provider_request(run, "a", 20, AttemptCost(None))
    assert unknown is not None and unknown.cost_micros is None and unknown.settled
    budget = await store.get(run)
    assert budget is not None
    assert (budget.usage.model_cost_micros, budget.usage.unpriced_requests) == (0, 1)
    with pytest.raises(BudgetExhausted) as error:
        await stores[-1].charge_provider_request(
            run, "b", 10, limits=COST_LIMITS, at=T0
        )
    assert error.value.resource is BudgetResource.MODEL_PRICE
    # Without a dollar limit an unknown price is recorded, not enforced.
    run = new_run()
    await store.charge_provider_request(run, "a", 10, limits=LIMITS, at=T0)
    await store.settle_provider_request(run, "a", 20, AttemptCost(None))
    await store.charge_provider_request(run, "b", 10, limits=LIMITS, at=T0)


async def in_flight_requests_may_overshoot_the_soft_limit(
    stores: Sequence[RunBudgetStore],
) -> None:
    """Spend is known only after a response: requests reserved before the
    limit is reached all complete, and their spend all counts."""
    run = new_run()
    reserved = [
        stores[i % len(stores)].charge_provider_request(
            run, f"r{i}", 10, limits=COST_LIMITS, at=T0
        )
        for i in range(3)
    ]
    accepted, refused = await _outcomes(reserved)
    assert (accepted, refused) == (3, [])
    for i in range(3):
        await stores[i % len(stores)].settle_provider_request(
            run, f"r{i}", 20, AttemptCost(900)
        )
    budget = await stores[0].get(run)
    assert budget is not None and budget.usage.model_cost_micros == 2700
    with pytest.raises(BudgetExhausted):
        await stores[0].charge_provider_request(
            run, "r9", 10, limits=COST_LIMITS, at=T0
        )


CONTRACT: tuple[Callable[[Sequence[RunBudgetStore]], Awaitable[None]], ...] = (
    limits_are_pinned_and_reopening_never_resets,
    concurrent_query_charges_never_exceed_the_query_limit,
    concurrent_retries_of_one_charge_count_once,
    concurrent_scans_never_exceed_the_run_byte_limit,
    settling_replaces_the_estimate_once,
    provider_requests_and_tokens_are_capped,
    corrections_are_capped_per_chain,
    clarification_time_is_excluded_from_active_time,
    model_spend_settles_once_and_stops_the_next_request,
    unknown_prices_are_never_free,
    in_flight_requests_may_overshoot_the_soft_limit,
)
