"""RunBudgets: the application gate over persisted run accounting."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from retail_analytics.application.budgets import (
    RetrySettings,
    RunBudgets,
)
from retail_analytics.application.contracts import Correlation
from retail_analytics.application.contracts.budgets import ProviderUsage
from retail_analytics.application.contracts.tools import ExecutionContext
from retail_analytics.application.contracts.warehouse_jobs import JobStatistics
from retail_analytics.application.ports.budgets import ProviderBudget
from retail_analytics.application.ports.query_execution import (
    QueryAdmission,
    QueryUsageRecorder,
)
from retail_analytics.application.query_execution import QueryNotAdmitted
from retail_analytics.domain.access import ProductScope
from retail_analytics.domain.budgets import (
    GIB,
    BudgetExhausted,
    BudgetResource,
    RunLimits,
)
from retail_analytics.domain.operations import ToolErrorCode
from tests.unit.budgets.memory_store import MemoryRunBudgetStore
from tests.unit.privacy.support import EXEC_A, SCOPE_A, compile_for

T0 = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
RUN = "run-1"
COMPILED = compile_for(EXEC_A, "SELECT product_id FROM products", SCOPE_A)


@dataclass
class Clock:
    now: datetime = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _budgets(
    store: MemoryRunBudgetStore,
    clock: Clock,
    limits: RunLimits | None = None,
    jitter: float = 0.0,
) -> RunBudgets:
    return RunBudgets(
        store,
        limits or RunLimits(),
        retry=RetrySettings(1.0, 20.0),
        clock=clock,
        jitter=lambda: jitter,
    )


def _context(run_id: str = RUN) -> ExecutionContext:
    return ExecutionContext(
        executive_id=EXEC_A,
        permissions=frozenset({"analysis:read"}),
        product_scope=ProductScope(frozenset({"1"}), 1),
        correlation=Correlation(session_id="ses-1", run_id=run_id),
    )


def test_run_budgets_implements_the_ports_t13_and_t14_use() -> None:
    budgets = _budgets(MemoryRunBudgetStore(), Clock())
    admission: QueryAdmission = budgets
    usage: QueryUsageRecorder = budgets
    provider: ProviderBudget = budgets
    assert {id(admission), id(usage), id(provider)} == {id(budgets)}


@pytest.mark.asyncio
async def test_admission_is_idempotent_per_submission_and_maps_refusals() -> None:
    store, clock = MemoryRunBudgetStore(), Clock()
    budgets = _budgets(store, clock)
    for _ in range(3):  # a retried activity admits the same submission again
        await budgets.admit(_context(), "op-1", 1, COMPILED, 1024)
    snapshot = await budgets.snapshot(RUN)
    assert snapshot is not None and snapshot.usage.queries == 1

    with pytest.raises(QueryNotAdmitted) as refused:
        await budgets.admit(_context(), "op-2", 1, COMPILED, GIB + 1)
    assert refused.value.code is ToolErrorCode.BUDGET_EXCEEDED
    assert refused.value.reason == "query_budget_query_bytes"
    assert "narrow" in refused.value.message

    for n in range(3, 12):
        await budgets.admit(_context(), f"op-{n}", 1, COMPILED, 1)
    with pytest.raises(QueryNotAdmitted) as refused:
        await budgets.admit(_context(), "op-99", 1, COMPILED, 1)
    assert refused.value.reason == "run_budget_queries"


@pytest.mark.asyncio
async def test_settlement_prefers_billed_then_processed_bytes() -> None:
    store, clock = MemoryRunBudgetStore(), Clock()
    budgets = _budgets(store, clock)
    await budgets.admit(_context(), "op-1", 1, COMPILED, 1000)
    await budgets.admit(_context(), "op-2", 1, COMPILED, 1000)
    await budgets.admit(_context(), "op-3", 1, COMPILED, 1000)
    await budgets.settle(RUN, "op-1", 1, JobStatistics(500, 10_485_760, False))
    await budgets.settle(RUN, "op-2", 1, JobStatistics(bytes_processed=2000))
    await budgets.settle(RUN, "op-3", 1, JobStatistics())
    await budgets.settle(RUN, "op-1", 1, JobStatistics(1, 1, False))  # repeated
    snapshot = await budgets.snapshot(RUN)
    assert snapshot is not None
    assert snapshot.usage.bytes == 10_485_760 + 2000 + 1000
    ambiguous = [c for c in await store.charges(RUN) if c.ambiguous]
    assert [c.key for c in ambiguous] == ["op-3#1"]


@pytest.mark.asyncio
async def test_provider_requests_are_reserved_then_settled() -> None:
    store, clock = MemoryRunBudgetStore(), Clock()
    budgets = _budgets(store, clock)
    permit = await budgets.reserve_provider_request(
        RUN, "turn-1/gemini/1", estimated_input_tokens=1200
    )
    assert permit.charged_tokens == 1200
    assert (permit.remaining_tokens, permit.remaining_requests) == (98_800, 19)
    # A retried reservation of the same request is not a second request.
    again = await budgets.reserve_provider_request(
        RUN, "turn-1/gemini/1", estimated_input_tokens=5
    )
    assert again.charged_tokens == 1200 and again.remaining_requests == 19
    await budgets.record_provider_usage(
        RUN, "turn-1/gemini/1", ProviderUsage(input_tokens=1100, output_tokens=400)
    )
    # Fallback to another provider is another request.
    await budgets.reserve_provider_request(
        RUN, "turn-1/openai/1", estimated_input_tokens=1000
    )
    await budgets.record_provider_usage(RUN, "turn-1/openai/1", ProviderUsage())
    snapshot = await budgets.snapshot(RUN)
    assert snapshot is not None
    assert (snapshot.usage.provider_requests, snapshot.usage.tokens) == (2, 2500)
    assert ProviderUsage(output_tokens=7).total == 7


@pytest.mark.asyncio
async def test_twenty_first_provider_request_is_refused() -> None:
    budgets = _budgets(MemoryRunBudgetStore(), Clock())
    for n in range(20):
        await budgets.reserve_provider_request(RUN, f"r{n}", estimated_input_tokens=0)
    with pytest.raises(BudgetExhausted) as error:
        await budgets.reserve_provider_request(RUN, "r20", estimated_input_tokens=0)
    assert error.value.resource is BudgetResource.PROVIDER_REQUESTS


@pytest.mark.asyncio
async def test_two_reformulations_per_failed_query() -> None:
    budgets = _budgets(MemoryRunBudgetStore(), Clock())
    await budgets.reserve_correction(RUN, "op-2", corrects="op-1")
    await budgets.reserve_correction(RUN, "op-3", corrects="op-2")
    with pytest.raises(BudgetExhausted) as error:
        await budgets.reserve_correction(RUN, "op-4", corrects="op-3")
    assert error.value.resource is BudgetResource.CORRECTIONS


@pytest.mark.asyncio
async def test_retry_decision_caps_attempts_and_respects_time() -> None:
    store, clock = MemoryRunBudgetStore(), Clock()
    budgets = _budgets(store, clock, jitter=0.5)
    first = await budgets.retry_decision(RUN, 1)
    assert first.allowed and first.delay_seconds == 0.75
    second = await budgets.retry_decision(RUN, 2, retry_after=4.0)
    assert second.allowed and second.delay_seconds == 4.0
    third = await budgets.retry_decision(RUN, 3)
    assert not third.allowed
    assert third.exhausted is BudgetResource.TRANSIENT_ATTEMPTS

    clock.advance(599.5)
    late = await budgets.retry_decision(RUN, 1)
    assert not late.allowed and late.exhausted is BudgetResource.ACTIVE_TIME


@pytest.mark.asyncio
async def test_resumed_worker_with_new_settings_keeps_the_pinned_budget() -> None:
    store, clock = MemoryRunBudgetStore(), Clock()
    first_worker = _budgets(store, clock)
    await first_worker.open(RUN)
    for n in range(10):
        await first_worker.admit(_context(), f"op-{n}", 1, COMPILED, 1)
    clock.advance(30)

    restarted = _budgets(store, clock, RunLimits(queries=100))
    snapshot = await restarted.open(RUN)
    assert snapshot.usage.queries == 10 and snapshot.limits.queries == 10
    assert snapshot.active_seconds == 30
    with pytest.raises(QueryNotAdmitted):
        await restarted.admit(_context(), "op-new", 1, COMPILED, 1)
    other_run = await restarted.open("run-2")
    assert other_run.limits.queries == 100


@pytest.mark.asyncio
async def test_clarification_wait_is_not_charged_but_other_waits_are() -> None:
    store, clock = MemoryRunBudgetStore(), Clock()
    budgets = _budgets(store, clock)
    await budgets.open(RUN)
    clock.advance(400)  # analysis, warehouse waits, backoff: all charged
    paused = await budgets.pause_for_clarification(RUN)
    assert paused.paused and paused.active_seconds == 400
    clock.advance(3600)  # the user takes an hour to answer
    resumed = await budgets.resume_after_clarification(RUN)
    assert not resumed.paused and resumed.active_seconds == 400
    clock.advance(199)
    await budgets.admit(_context(), "op-1", 1, COMPILED, 1)
    clock.advance(1)
    with pytest.raises(QueryNotAdmitted) as refused:
        await budgets.admit(_context(), "op-2", 1, COMPILED, 1)
    assert refused.value.reason == "run_budget_active_time"


@pytest.mark.asyncio
async def test_context_carries_a_read_only_budget_snapshot() -> None:
    budgets = _budgets(MemoryRunBudgetStore(), Clock())
    assert (await budgets.with_budget(_context())).budget is None
    await budgets.admit(_context(), "op-1", 1, COMPILED, 2048)
    context = await budgets.with_budget(_context())
    assert context.budget is not None
    assert context.budget.usage.queries == 1
    remaining = context.budget.remaining()
    assert remaining[BudgetResource.QUERIES] == 9
    assert remaining[BudgetResource.RUN_BYTES] == 5 * GIB - 2048
    assert context.budget.exhausted() == frozenset()
