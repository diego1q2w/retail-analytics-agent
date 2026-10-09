"""Pure run budget rules: defaults, boundaries, settlement, clock and backoff."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from retail_analytics.domain.budgets import (
    GIB,
    BudgetExhausted,
    BudgetResource,
    ChargeKind,
    RunBudget,
    RunLimits,
    backoff_delay,
)

T0 = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


def _fresh(**limits: int) -> RunBudget:
    return RunBudget.open("run-1", RunLimits(**limits), at=T0)


def _refused(resource: BudgetResource) -> pytest.RaisesExc[BudgetExhausted]:
    return pytest.raises(BudgetExhausted, match=resource.value)


def test_defaults_match_the_accepted_operational_limits() -> None:
    limits = RunLimits()
    assert limits.active_seconds == 120
    assert (limits.provider_requests, limits.tokens) == (20, 100_000)
    assert limits.queries == 10
    assert (limits.bytes_per_query, limits.bytes_per_run) == (GIB, 5 * GIB)
    assert (limits.corrections_per_query, limits.transient_attempts) == (2, 3)
    assert limits.query_deadline_seconds == 120
    assert (limits.result_rows, limits.result_bytes) == (500, 256 * 1024)
    assert RunLimits.from_dict(limits.as_dict()) == limits
    assert RunLimits.from_dict({**limits.as_dict(), "retired": 1}) == limits


def test_invalid_limits_are_rejected() -> None:
    with pytest.raises(ValueError, match="exceeds"):
        RunLimits(bytes_per_query=2 * GIB, bytes_per_run=GIB)
    with pytest.raises(ValueError, match="negative"):
        RunLimits(queries=-1)
    with pytest.raises(ValueError, match="attempt"):
        RunLimits(transient_attempts=0)


def test_tenth_query_is_admitted_and_eleventh_refused() -> None:
    budget = _fresh()
    for n in range(10):
        budget, _ = budget.charge_query(f"op-{n}#1", 1, at=T0)
    assert budget.usage.queries == 10
    with _refused(BudgetResource.QUERIES):
        budget.charge_query("op-10#1", 0, at=T0)


def test_query_and_run_byte_boundaries() -> None:
    budget = _fresh()
    budget, charge = budget.charge_query("a#1", GIB, at=T0)  # exactly 1 GiB
    assert charge.bytes == GIB and charge.kind is ChargeKind.QUERY
    with _refused(BudgetResource.QUERY_BYTES):
        budget.charge_query("b#1", GIB + 1, at=T0)
    for n in range(3):
        budget, _ = budget.charge_query(f"c{n}#1", GIB, at=T0)
    budget, _ = budget.charge_query("d#1", GIB - 1, at=T0)
    assert budget.usage.bytes == 5 * GIB - 1
    budget, _ = budget.charge_query("e#1", 1, at=T0)  # exactly 5 GiB
    with _refused(BudgetResource.RUN_BYTES):
        budget.charge_query("f#1", 1, at=T0)
    assert BudgetResource.RUN_BYTES in budget.snapshot(T0).exhausted()


def test_settlement_uses_actual_bytes_even_above_the_estimate() -> None:
    budget, charge = _fresh().charge_query("a#1", 100, at=T0)
    budget, settled = budget.settle_query(charge, 4 * GIB + 1)
    assert budget.usage.bytes == 4 * GIB + 1 and settled.settled
    assert budget.settle_query(settled, 1) == (budget, settled)
    with _refused(BudgetResource.RUN_BYTES):
        budget.charge_query("b#1", GIB, at=T0)
    budget, charge = budget.charge_query("c#1", 10, at=T0)
    budget, unknown = budget.settle_query(charge, None)
    assert unknown.ambiguous and budget.usage.bytes == 4 * GIB + 11


def test_provider_request_and_token_boundaries() -> None:
    budget = _fresh()
    for n in range(20):
        budget, _ = budget.charge_provider_request(f"r{n}", 0, at=T0)
    with _refused(BudgetResource.PROVIDER_REQUESTS):
        budget.charge_provider_request("r20", 0, at=T0)

    budget, charge = _fresh().charge_provider_request("a", 100_000, at=T0)
    assert budget.usage.tokens == 100_000
    with _refused(BudgetResource.TOKENS):
        budget.charge_provider_request("b", 0, at=T0)
    budget, settled = budget.settle_provider_request(charge, 60_000)
    assert budget.usage.tokens == 60_000 and settled.tokens == 60_000
    with _refused(BudgetResource.TOKENS):
        budget.charge_provider_request("c", 40_001, at=T0)
    budget, _ = budget.charge_provider_request("c", 40_000, at=T0)


def test_active_time_boundary_and_clarification_pause() -> None:
    budget = _fresh(active_seconds=600)
    budget.charge_query("a#1", 1, at=T0 + timedelta(seconds=599))
    with _refused(BudgetResource.ACTIVE_TIME):
        budget.charge_query("a#1", 1, at=T0 + timedelta(seconds=600))
    paused = budget.pause(at=T0 + timedelta(seconds=100))
    assert paused.pause(at=T0 + timedelta(seconds=500)) == paused
    resumed = paused.resume(at=T0 + timedelta(days=1))
    assert resumed.resume(at=T0 + timedelta(days=2)) == resumed
    later = T0 + timedelta(days=1, seconds=499)
    assert resumed.snapshot(later).active_seconds == 599
    resumed.charge_query("b#1", 1, at=later)
    with _refused(BudgetResource.ACTIVE_TIME):
        resumed.charge_provider_request("r", 1, at=later + timedelta(seconds=1))


def test_corrections_per_chain_boundary() -> None:
    budget = _fresh()
    assert budget.charge_correction("op-2", "op-1", 1, at=T0).group == "op-1"
    with _refused(BudgetResource.CORRECTIONS):
        budget.charge_correction("op-3", "op-1", 2, at=T0)
    with _refused(BudgetResource.CORRECTIONS):
        _fresh(corrections_per_query=0).charge_correction("x", "y", 0, at=T0)


def test_exhaustion_scope() -> None:
    assert BudgetExhausted(BudgetResource.RUN_BYTES).run_exhausted
    assert BudgetExhausted(BudgetResource.ACTIVE_TIME).run_exhausted
    assert not BudgetExhausted(BudgetResource.QUERY_BYTES).run_exhausted
    assert not BudgetExhausted(BudgetResource.CORRECTIONS).run_exhausted


def test_backoff_grows_exponentially_with_bounded_jitter() -> None:
    def delay(failures: int, jitter: float, after: float | None = None) -> float:
        return backoff_delay(
            failures,
            base_seconds=1.0,
            max_seconds=20.0,
            jitter=jitter,
            retry_after=after,
        )

    assert [delay(n, 0.0) for n in (1, 2, 3)] == [0.5, 1.0, 2.0]
    assert delay(2, 0.999) < 2.0
    assert delay(10, 0.0) == 10.0  # capped at max_seconds/2..max_seconds
    assert delay(1, 0.0, after=7.5) == 7.5
    assert delay(3, 0.5, after=0.1) == 3.0
    with pytest.raises(ValueError):
        delay(0, 0.0)
    with pytest.raises(ValueError):
        delay(1, 1.0)
