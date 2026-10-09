"""What an investigation does next: lifecycle policy independent of any runtime.

An execution runtime (today the Temporal workflow) runs the steps of
``InvestigationRuntime`` and the agent loop; after each one it asks this module
what follows and carries the decision out with its own primitives. The policy
lives here once, so a runtime supplies execution, not a second copy of the
rules.

Everything here is pure and deterministic - no I/O, clock or randomness - so a
replaying workflow may call it.
"""

from __future__ import annotations

from datetime import timedelta

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
from retail_analytics.domain.request_scope import AdmissionDecision
from retail_analytics.domain.runs import RunStatus

# A clarification waits at most this long for the user; then the run expires.
WAIT_LIMIT = timedelta(days=7)
# Cancellation re-checks unsettled effects this often, this many times, before
# finishing with whatever could not be confirmed.
CANCEL_SETTLE_CHECKS = 60
CANCEL_SETTLE_INTERVAL = timedelta(seconds=2)
DECLINED_MESSAGE = "Start a new topic."
CLARIFY_MESSAGE = "What would you like to analyze?"

_INVESTIGATE = LifecycleDecision(LifecycleAction.INVESTIGATE)
_AWAIT_INPUT = LifecycleDecision(LifecycleAction.AWAIT_INPUT)
_CANCEL = LifecycleDecision(LifecycleAction.CANCEL)
_CLOSE = LifecycleDecision(LifecycleAction.CLOSE)


def admit(beginning: BeginOutcome) -> LifecycleDecision:
    """After ``InvestigationRuntime.begin``: close, finish, ask or investigate.

    ``cancelling`` reports a run whose cancellation was requested before it
    began; the admission step still completes, then the run is cancelled.
    """
    if beginning.status is None or beginning.status.is_terminal:
        return _CLOSE
    cancelling = beginning.status is RunStatus.CANCELLING
    if beginning.admission in (
        AdmissionDecision.DECLINE,
        AdmissionDecision.RESET_TOPIC,
    ):
        return LifecycleDecision(
            LifecycleAction.FINISH_MESSAGE,
            beginning.message or DECLINED_MESSAGE,
            cancelling=cancelling,
        )
    if beginning.admission is AdmissionDecision.CLARIFY:
        return LifecycleDecision(
            LifecycleAction.ASK,
            beginning.message or CLARIFY_MESSAGE,
            cancelling=cancelling,
        )
    if beginning.status is RunStatus.WAITING_FOR_INPUT:
        return LifecycleDecision(LifecycleAction.AWAIT_INPUT, cancelling=cancelling)
    return LifecycleDecision(LifecycleAction.INVESTIGATE, cancelling=cancelling)


def after_admission_question(outcome: StepOutcome) -> LifecycleDecision:
    """After asking the admission question: wait for the answer if it was asked."""
    return _AWAIT_INPUT if outcome.result is StepResult.ASKED else _INVESTIGATE


def after_resume(outcome: StepOutcome) -> LifecycleDecision:
    """After ``InvestigationRuntime.resume`` while waiting for input."""
    if outcome.result is StepResult.STOPPED:
        return _CANCEL
    if outcome.result is StepResult.IDLE:
        return LifecycleDecision(LifecycleAction.WAIT)
    return _INVESTIGATE


def after_wait_expired(outcome: StepOutcome) -> LifecycleDecision:
    """After ``InvestigationRuntime.expire``: input that arrived meanwhile wins."""
    if outcome.result is StepResult.SUPERSEDED:
        return _AWAIT_INPUT
    return LifecycleDecision(LifecycleAction.EXPIRE)


def after_interruption(interruption: AgentInterruption) -> LifecycleDecision:
    """After an agent run ended without an answer or a question."""
    if interruption.kind is InterruptionKind.CONTEXT_CHANGED:
        # Durable run budgets and tool effects survive this fresh
        # conversation; unsafe provider history does not.
        return _INVESTIGATE
    stopped = interruption.kind is InterruptionKind.STOPPED
    if stopped and interruption.reason is StopReason.CANCELLED:
        return _CANCEL
    return LifecycleDecision(
        LifecycleAction.STOP,
        stop_reason=(
            interruption.reason
            if stopped and interruption.reason is not None
            else StopReason.MODEL_UNAVAILABLE
        ),
        resource=interruption.resource if stopped else None,
    )


def after_output(outcome: StepOutcome) -> LifecycleDecision:
    """After releasing an answer or asking a question the model proposed.

    Anything but a release, a stop or an open question (withheld output,
    superseding input) continues the investigation with rebuilt context.
    """
    if outcome.result is StepResult.RELEASED:
        return _CLOSE
    if outcome.result is StepResult.STOPPED:
        if outcome.stop_reason is StopReason.CANCELLED:
            return _CANCEL
        return LifecycleDecision(
            LifecycleAction.STOP,
            stop_reason=outcome.stop_reason or StopReason.INTERRUPTED,
        )
    if outcome.result is StepResult.ASKED:
        return _AWAIT_INPUT
    return _INVESTIGATE


def cancellation_settled(progress: CancelProgress) -> bool:
    return progress.unsettled == 0
