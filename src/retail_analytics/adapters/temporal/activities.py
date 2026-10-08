"""Activity boundary for durable investigation state changes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from retail_analytics.application.investigation_runtime import (
    AnswerDraft,
    BeginOutcome,
    CancelProgress,
    FinishRequest,
    InvestigationRuntime,
    QuestionDraft,
    StepOutcome,
)

_runtime: InvestigationRuntime | None = None


def bind_runtime(runtime: InvestigationRuntime) -> None:
    global _runtime
    _runtime = runtime


def bound() -> InvestigationRuntime:
    if _runtime is None:
        raise ApplicationError("runtime unbound", type="Unbound", non_retryable=True)
    return _runtime


@dataclass(frozen=True)
class CancelRequest:
    run_id: str
    settled: bool


@dataclass(frozen=True)
class MessageRequest:
    run_id: str
    message: str


@activity.defn
async def begin(run_id: str) -> BeginOutcome:
    return await bound().begin(run_id)


@activity.defn
async def release_answer(draft: AnswerDraft) -> StepOutcome:
    return await bound().release_answer(draft)


@activity.defn
async def ask(draft: QuestionDraft) -> StepOutcome:
    return await bound().ask(draft)


@activity.defn
async def resume(run_id: str) -> StepOutcome:
    return await bound().resume(run_id)


@activity.defn
async def finish(request: FinishRequest) -> StepOutcome:
    return await bound().finish_partial(request)


@activity.defn
async def finish_message(request: MessageRequest) -> StepOutcome:
    return await bound().finish_message(request.run_id, request.message)


@activity.defn
async def expire(run_id: str) -> StepOutcome:
    return await bound().expire(run_id)


@activity.defn
async def begin_cancel(run_id: str) -> CancelProgress:
    return await bound().begin_cancel(run_id)


@activity.defn
async def reconcile_cancel(run_id: str) -> CancelProgress:
    return await bound().reconcile_cancel(run_id)


@activity.defn
async def finish_cancelled(request: CancelRequest) -> StepOutcome:
    return await bound().finish_cancelled(request.run_id, settled=request.settled)


REGISTERED: list[Any] = [
    begin,
    release_answer,
    ask,
    resume,
    finish,
    finish_message,
    expire,
    begin_cancel,
    reconcile_cancel,
    finish_cancelled,
]
