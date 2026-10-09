"""The application's investigation lifecycle policy, decision by decision."""

from __future__ import annotations

import pytest

from retail_analytics.application import investigation_lifecycle as lifecycle
from retail_analytics.application.contracts.investigations import (
    AgentInterruption,
    BeginOutcome,
    CancelProgress,
    InterruptionKind,
    LifecycleAction,
    LifecycleDecision,
    StepOutcome,
    StepResult,
    StopReason,
)
from retail_analytics.domain.budgets import BudgetResource
from retail_analytics.domain.request_scope import AdmissionDecision
from retail_analytics.domain.runs import RunStatus

A = LifecycleAction


@pytest.mark.parametrize(
    ("beginning", "expected"),
    [
        (BeginOutcome(None), LifecycleDecision(A.CLOSE)),
        (BeginOutcome(RunStatus.COMPLETED), LifecycleDecision(A.CLOSE)),
        (
            BeginOutcome(RunStatus.RUNNING, AdmissionDecision.PROCEED),
            LifecycleDecision(A.INVESTIGATE),
        ),
        (
            BeginOutcome(RunStatus.WAITING_FOR_INPUT, AdmissionDecision.PROCEED),
            LifecycleDecision(A.AWAIT_INPUT),
        ),
        (
            BeginOutcome(RunStatus.CANCELLING, AdmissionDecision.PROCEED),
            LifecycleDecision(A.INVESTIGATE, cancelling=True),
        ),
        (
            BeginOutcome(RunStatus.RUNNING, AdmissionDecision.DECLINE, "Off topic."),
            LifecycleDecision(A.FINISH_MESSAGE, "Off topic."),
        ),
        (
            BeginOutcome(RunStatus.RUNNING, AdmissionDecision.RESET_TOPIC),
            LifecycleDecision(A.FINISH_MESSAGE, lifecycle.DECLINED_MESSAGE),
        ),
        (
            BeginOutcome(RunStatus.RUNNING, AdmissionDecision.CLARIFY),
            LifecycleDecision(A.ASK, lifecycle.CLARIFY_MESSAGE),
        ),
    ],
)
def test_admission(beginning: BeginOutcome, expected: LifecycleDecision) -> None:
    assert lifecycle.admit(beginning) == expected


@pytest.mark.parametrize(
    ("result", "resumed"),
    [
        (StepResult.STOPPED, A.CANCEL),
        (StepResult.IDLE, A.WAIT),
        (StepResult.CONTINUE, A.INVESTIGATE),
    ],
)
def test_waiting_for_input(result: StepResult, resumed: LifecycleAction) -> None:
    assert lifecycle.after_resume(StepOutcome(result)).action is resumed


def test_expired_wait_keeps_input_that_arrived_meanwhile() -> None:
    superseded = StepOutcome(StepResult.SUPERSEDED)
    assert lifecycle.after_wait_expired(superseded).action is A.AWAIT_INPUT
    expired = StepOutcome(StepResult.STOPPED)
    assert lifecycle.after_wait_expired(expired).action is A.EXPIRE
    assert lifecycle.WAIT_LIMIT.days == 7


@pytest.mark.parametrize(
    ("interruption", "expected"),
    [
        (
            AgentInterruption(InterruptionKind.CONTEXT_CHANGED),
            LifecycleDecision(A.INVESTIGATE),
        ),
        (
            AgentInterruption(InterruptionKind.STOPPED, StopReason.CANCELLED),
            LifecycleDecision(A.CANCEL),
        ),
        (
            AgentInterruption(
                InterruptionKind.STOPPED, StopReason.BUDGET, BudgetResource.QUERIES
            ),
            LifecycleDecision(
                A.STOP, stop_reason=StopReason.BUDGET, resource=BudgetResource.QUERIES
            ),
        ),
        (
            AgentInterruption(InterruptionKind.FAILED),
            LifecycleDecision(A.STOP, stop_reason=StopReason.MODEL_UNAVAILABLE),
        ),
    ],
)
def test_interrupted_agent(
    interruption: AgentInterruption, expected: LifecycleDecision
) -> None:
    assert lifecycle.after_interruption(interruption) == expected


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (StepOutcome(StepResult.RELEASED), LifecycleDecision(A.CLOSE)),
        (StepOutcome(StepResult.ASKED), LifecycleDecision(A.AWAIT_INPUT)),
        (StepOutcome(StepResult.WITHHELD), LifecycleDecision(A.INVESTIGATE)),
        (StepOutcome(StepResult.SUPERSEDED), LifecycleDecision(A.INVESTIGATE)),
        (
            StepOutcome(StepResult.STOPPED, stop_reason=StopReason.CANCELLED),
            LifecycleDecision(A.CANCEL),
        ),
        (
            StepOutcome(StepResult.STOPPED, stop_reason=StopReason.ACCESS),
            LifecycleDecision(A.STOP, stop_reason=StopReason.ACCESS),
        ),
        (
            StepOutcome(StepResult.STOPPED),
            LifecycleDecision(A.STOP, stop_reason=StopReason.INTERRUPTED),
        ),
    ],
)
def test_released_or_asked(outcome: StepOutcome, expected: LifecycleDecision) -> None:
    assert lifecycle.after_output(outcome) == expected


def test_admission_question() -> None:
    asked = StepOutcome(StepResult.ASKED)
    assert lifecycle.after_admission_question(asked).action is A.AWAIT_INPUT
    superseded = StepOutcome(StepResult.SUPERSEDED)
    assert lifecycle.after_admission_question(superseded).action is A.INVESTIGATE


def test_cancellation_settles_only_when_nothing_is_unconfirmed() -> None:
    assert lifecycle.cancellation_settled(CancelProgress(0))
    assert not lifecycle.cancellation_settled(CancelProgress(1))
