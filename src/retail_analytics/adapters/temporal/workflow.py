"""One adaptive agent, durable waits and cooperative cancellation per run."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import timedelta
from functools import partial
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from pydantic_ai.durable_exec.temporal import PydanticAIWorkflow
    from pydantic_ai.exceptions import AgentRunError
    from pydantic_ai.usage import UsageLimits

    from retail_analytics.adapters.temporal import activities
    from retail_analytics.adapters.temporal.agent import (
        AnswerOutput,
        InvestigationDeps,
        investigation_agent,
        is_context_changed,
        is_run_stopped,
    )
    from retail_analytics.application.investigation_runtime import (
        AnswerDraft,
        FinishRequest,
        QuestionDraft,
        StepResult,
        StopReason,
    )
    from retail_analytics.application.telemetry import (
        ATTRIBUTION_METADATA_KEY,
        attribution_from_metadata,
    )
    from retail_analytics.domain.budgets import BudgetResource
    from retail_analytics.domain.request_scope import AdmissionDecision
    from retail_analytics.domain.runs import RunStatus

_OPTIONS: dict[str, Any] = {
    "start_to_close_timeout": timedelta(seconds=30),
    "retry_policy": RetryPolicy(
        maximum_attempts=5, non_retryable_error_types=["Unbound"]
    ),
}
WAIT_LIMIT = timedelta(days=7)


@workflow.defn
class InvestigationWorkflow(PydanticAIWorkflow):
    __pydantic_ai_agents__ = (investigation_agent,)

    def __init__(self) -> None:
        self._input_generation = 0
        self._cancelled = False

    @workflow.signal
    def input_available(self) -> None:
        self._input_generation += 1

    @workflow.signal
    def cancel_requested(self) -> None:
        self._cancelled = True

    def _is_cancelled(self) -> bool:
        return self._cancelled

    def _input_changed(self, generation: int) -> bool:
        return self._cancelled or self._input_generation != generation

    def _work_done(self, task: asyncio.Future[Any]) -> bool:
        return self._cancelled or task.done()

    @workflow.run
    async def run(self, run_id: str) -> str:
        beginning = await workflow.execute_activity(
            activities.begin, run_id, **_OPTIONS
        )
        if beginning.status is None or beginning.status.is_terminal:
            return "closed"
        if beginning.status is RunStatus.CANCELLING:
            self._cancelled = True
        if beginning.admission in (
            AdmissionDecision.DECLINE,
            AdmissionDecision.RESET_TOPIC,
        ):
            await workflow.execute_activity(
                activities.finish_message,
                activities.MessageRequest(
                    run_id, beginning.message or "Start a new topic."
                ),
                **_OPTIONS,
            )
            return "closed"
        waiting = beginning.status is RunStatus.WAITING_FOR_INPUT
        sequence = 0
        if beginning.admission is AdmissionDecision.CLARIFY:
            outcome = await workflow.execute_activity(
                activities.ask,
                QuestionDraft(
                    run_id,
                    sequence,
                    beginning.message or "What would you like to analyze?",
                ),
                **_OPTIONS,
            )
            waiting = outcome.result is StepResult.ASKED
            sequence += 1
        while not self._is_cancelled():
            if waiting:
                # Check persisted input before sleeping: notifications arriving
                # during the preceding activity cannot be lost.
                generation = self._input_generation
                outcome = await workflow.execute_activity(
                    activities.resume, run_id, **_OPTIONS
                )
                if outcome.result is StepResult.STOPPED:
                    self._cancelled = True
                    break
                if outcome.result is StepResult.IDLE:
                    try:
                        await workflow.wait_condition(
                            partial(self._input_changed, generation),
                            timeout=WAIT_LIMIT,
                        )
                    except TimeoutError:
                        expiry = await workflow.execute_activity(
                            activities.expire, run_id, **_OPTIONS
                        )
                        if expiry.result is StepResult.SUPERSEDED:
                            continue
                        return "expired"
                    continue
                waiting = False
            task = asyncio.create_task(
                investigation_agent.run(
                    "Investigate the persisted request supplied by "
                    "the activity context.",
                    deps=InvestigationDeps(run_id=run_id),
                    usage_limits=UsageLimits(request_limit=25, tool_calls_limit=100),
                )
            )
            await workflow.wait_condition(partial(self._work_done, task))
            if self._is_cancelled():
                task.cancel()
                with suppress(asyncio.CancelledError, ActivityError, AgentRunError):
                    await task
                break
            try:
                result = await task
            except (ActivityError, AgentRunError) as error:
                if is_context_changed(error):
                    # Durable run budgets and tool effects survive this fresh
                    # conversation; unsafe provider history does not.
                    continue
                stopped = is_run_stopped(error)
                if stopped and stopped[0] == StopReason.CANCELLED.value:
                    self._cancelled = True
                    break
                reason = (
                    StopReason(stopped[0]) if stopped else StopReason.MODEL_UNAVAILABLE
                )
                resource = (
                    BudgetResource(stopped[1]) if stopped and stopped[1] else None
                )
                await workflow.execute_activity(
                    activities.finish,
                    FinishRequest(run_id, reason, resource),
                    **_OPTIONS,
                )
                return "stopped"
            output = result.output
            if isinstance(output, AnswerOutput):
                outcome = await workflow.execute_activity(
                    activities.release_answer,
                    AnswerDraft(
                        run_id,
                        sequence,
                        output.text,
                        tuple(output.cited_evidence),
                        output.complete,
                        attribution_from_metadata(
                            (result.response.metadata or {}).get(
                                ATTRIBUTION_METADATA_KEY
                            )
                        ),
                    ),
                    **_OPTIONS,
                )
            else:
                outcome = await workflow.execute_activity(
                    activities.ask,
                    QuestionDraft(run_id, sequence, output.question),
                    **_OPTIONS,
                )
            sequence += 1
            if outcome.result is StepResult.RELEASED:
                return "closed"
            if outcome.result is StepResult.STOPPED:
                if outcome.stop_reason is StopReason.CANCELLED:
                    self._cancelled = True
                    break
                await workflow.execute_activity(
                    activities.finish,
                    FinishRequest(
                        run_id, outcome.stop_reason or StopReason.INTERRUPTED
                    ),
                    **_OPTIONS,
                )
                return "stopped"
            waiting = outcome.result is StepResult.ASKED
            # A fresh agent run receives rebuilt context. Old model messages
            # cannot carry revoked evidence or superseded assumptions forward.
        progress = await workflow.execute_activity(
            activities.begin_cancel, run_id, **_OPTIONS
        )
        for _ in range(60):
            if progress.unsettled == 0:
                break
            await asyncio.sleep(2)
            progress = await workflow.execute_activity(
                activities.reconcile_cancel, run_id, **_OPTIONS
            )
        await workflow.execute_activity(
            activities.finish_cancelled,
            activities.CancelRequest(run_id, progress.unsettled == 0),
            **_OPTIONS,
        )
        return "cancelled"
