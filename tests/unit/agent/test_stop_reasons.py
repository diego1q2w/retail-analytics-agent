"""The exhausted budget resource survives every wrapper up to the user text.

A provider reservation refused for tokens raises ``RunStopped(BUDGET,
TOKENS)``; agent, task-group and Temporal activity wrappers must not lose the
resource, and the stop message must name it (the earlier text fell back to a
generic "budget for this kind of work" for tokens and model requests).
"""

from __future__ import annotations

import pytest
from temporalio.exceptions import ActivityError, RetryState

from retail_analytics.adapters.local.investigations import (
    interruption as local_interruption,
)
from retail_analytics.adapters.temporal import agent as temporal
from retail_analytics.application import investigation_lifecycle as lifecycle
from retail_analytics.application.contracts.investigations import (
    InterruptionKind,
    LifecycleAction,
    StopReason,
)
from retail_analytics.application.investigation_runtime import (
    RunStopped,
    stop_message,
)
from retail_analytics.domain.budgets import BudgetResource


def _wrapped_locally(stopped: RunStopped) -> BaseException:
    try:
        try:
            raise stopped
        except RunStopped as error:
            raise RuntimeError("agent run failed") from error
    except RuntimeError as outer:
        return ExceptionGroup("task group", [ValueError("other"), outer])


def _wrapped_by_temporal(stopped: RunStopped) -> BaseException:
    error = ActivityError(
        "activity failed",
        scheduled_event_id=1,
        started_event_id=2,
        identity="worker",
        activity_type="model",
        activity_id="1",
        retry_state=RetryState.NON_RETRYABLE_FAILURE,
    )
    error.__cause__ = temporal.stopped_error(stopped)
    outer = RuntimeError("model request failed")
    outer.__cause__ = error
    return outer


@pytest.mark.parametrize("wrap", [_wrapped_locally, _wrapped_by_temporal])
@pytest.mark.parametrize(
    "resource",
    [
        BudgetResource.TOKENS,
        BudgetResource.PROVIDER_REQUESTS,
        BudgetResource.ACTIVE_TIME,
    ],
)
def test_budget_resource_survives_wrappers_to_the_stop_decision(
    wrap: object, resource: BudgetResource
) -> None:
    error = wrap(RunStopped(StopReason.BUDGET, resource))  # type: ignore[operator]
    mapped = (
        temporal.interruption(error)
        if wrap is _wrapped_by_temporal
        else local_interruption(error)
    )
    assert mapped.kind is InterruptionKind.STOPPED
    decision = lifecycle.after_interruption(mapped)
    assert decision.action is LifecycleAction.STOP
    assert (decision.stop_reason, decision.resource) == (StopReason.BUDGET, resource)


@pytest.mark.parametrize(
    ("resource", "phrase"),
    [
        (BudgetResource.TOKENS, "model token budget"),
        (BudgetResource.PROVIDER_REQUESTS, "model request budget"),
        (BudgetResource.ACTIVE_TIME, "time budget"),
        (BudgetResource.QUERIES, "query budget"),
        (BudgetResource.RUN_BYTES, "data-scan budget"),
    ],
)
def test_stop_message_names_the_exhausted_resource(
    resource: BudgetResource, phrase: str
) -> None:
    message = stop_message(StopReason.BUDGET, resource)
    assert phrase in message
    assert "this kind of work" not in message
    # A budget stop is not a truncated result.
    assert "truncat" not in message.casefold()
    assert "cut off" not in message


def test_stop_without_a_known_resource_stays_truthful() -> None:
    assert "budget" in stop_message(StopReason.BUDGET, None)
    assert "unavailable" in stop_message(StopReason.MODEL_UNAVAILABLE, None)
    assert "access changed" in stop_message(StopReason.ACCESS, None)
