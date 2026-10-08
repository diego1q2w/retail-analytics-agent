"""Run budgets on real PostgreSQL: the store contract, with concurrent
reservations from separate connection pools (as separate workers would)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator, Sequence

import pytest

from retail_analytics.application.budgets import RunBudgets
from retail_analytics.application.ports.budgets import RunBudgetStore
from retail_analytics.bootstrap.persistence import Persistence, build_persistence
from retail_analytics.domain.budgets import BudgetExhausted, RunLimits
from tests.integration.compose_stack import Stack, running_stack
from tests.unit.budgets.contract import CONTRACT, LIMITS, T0, new_run

pytestmark = pytest.mark.docker


@pytest.fixture(scope="module")
def stack() -> Iterator[Stack]:
    with running_stack("postgres") as stack:
        stack.migrate()
        yield stack


@pytest.fixture
def workers(stack: Stack) -> Iterator[list[Persistence]]:
    pools = [build_persistence(stack.app_url) for _ in range(3)]
    yield pools
    for pool in pools:
        pool.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CONTRACT, ids=lambda case: case.__name__)
async def test_postgres_store_contract(
    workers: list[Persistence],
    case: Callable[[Sequence[RunBudgetStore]], Awaitable[None]],
) -> None:
    await case([w.budgets for w in workers])


@pytest.mark.asyncio
async def test_retried_admissions_across_workers_charge_one_query(
    workers: list[Persistence],
) -> None:
    run = new_run()
    keys = [f"op-{n}#1" for n in range(12)]
    sent = [keys[i % 12] for i in range(72)]  # every key retried six times
    calls = [
        workers[i % 3].budgets.charge_query(run, key, 1, limits=LIMITS, at=T0)
        for i, key in enumerate(sent)
    ]
    results = await asyncio.gather(*calls, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException) and not isinstance(
            result, BudgetExhausted
        ):
            raise result
    budget = await workers[1].budgets.get(run)
    assert budget is not None and budget.usage.queries == 10
    accepted = {c.key for c in await workers[0].budgets.charges(run)}
    assert len(accepted) == 10
    # Every retry of an accepted key succeeded; every other key was refused.
    for key, result in zip(sent, results, strict=True):
        assert isinstance(result, BudgetExhausted) == (key not in accepted)


@pytest.mark.asyncio
async def test_budget_survives_a_new_process_with_other_settings(
    stack: Stack, workers: list[Persistence]
) -> None:
    run = new_run()
    first = RunBudgets(workers[0].budgets, LIMITS, clock=lambda: T0)
    await first.open(run)
    for n in range(3):
        await first.reserve_provider_request(run, f"r{n}", estimated_input_tokens=10)
    restarted = build_persistence(stack.app_url)
    try:
        later = RunBudgets(
            restarted.budgets, RunLimits(provider_requests=500), clock=lambda: T0
        )
        snapshot = await later.open(run)
        assert snapshot.limits == LIMITS
        assert (snapshot.usage.provider_requests, snapshot.usage.tokens) == (3, 30)
    finally:
        restarted.close()
