"""One adaptive agent, durable waits and cooperative cancellation per run.

The workflow executes; it does not decide. Every step's outcome goes to the
application's lifecycle policy (``application.investigation_lifecycle``),
and the workflow carries the decision out with Temporal primitives:
activities, signals, durable timers and the durable agent. Policy calls are
pure, so replay stays deterministic.
"""

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

    from retail_analytics.adapters.agent.investigator import (
        proposal,
        run_investigation,
    )
    from retail_analytics.adapters.temporal import activities
    from retail_analytics.adapters.temporal.agent import (
        UNBOUND,
        interruption,
        investigation_agent,
    )
    from retail_analytics.application import investigation_lifecycle as lifecycle
    from retail_analytics.application.contracts.investigations import (
        AnswerDraft,
        FinishRequest,
        LifecycleAction,
        LifecycleDecision,
        QuestionDraft,
        StopReason,
    )

_OPTIONS: dict[str, Any] = {
    "start_to_close_timeout": timedelta(seconds=30),
    "retry_policy": RetryPolicy(
        maximum_attempts=5, non_retryable_error_types=[UNBOUND]
    ),
}
WAIT_LIMIT = lifecycle.WAIT_LIMIT


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
        admission = lifecycle.admit(
            await workflow.execute_activity(activities.begin, run_id, **_OPTIONS)
        )
        if admission.action is LifecycleAction.CLOSE:
            return "closed"
        if admission.cancelling:
            self._cancelled = True
        if admission.action is LifecycleAction.FINISH_MESSAGE:
            await workflow.execute_activity(
                activities.finish_message,
                activities.MessageRequest(run_id, admission.message or ""),
                **_OPTIONS,
            )
            return "closed"
        waiting = admission.action is LifecycleAction.AWAIT_INPUT
        sequence = 0
        if admission.action is LifecycleAction.ASK:
            asked = await workflow.execute_activity(
                activities.ask,
                QuestionDraft(run_id, sequence, admission.message or ""),
                **_OPTIONS,
            )
            waiting = (
                lifecycle.after_admission_question(asked).action
                is LifecycleAction.AWAIT_INPUT
            )
            sequence += 1
        while not self._is_cancelled():
            if waiting:
                # Check persisted input before sleeping: notifications arriving
                # during the preceding activity cannot be lost.
                generation = self._input_generation
                resumed = lifecycle.after_resume(
                    await workflow.execute_activity(
                        activities.resume, run_id, **_OPTIONS
                    )
                )
                if resumed.action is LifecycleAction.CANCEL:
                    self._cancelled = True
                    break
                if resumed.action is LifecycleAction.WAIT:
                    try:
                        await workflow.wait_condition(
                            partial(self._input_changed, generation),
                            timeout=WAIT_LIMIT,
                        )
                    except TimeoutError:
                        expiry = lifecycle.after_wait_expired(
                            await workflow.execute_activity(
                                activities.expire, run_id, **_OPTIONS
                            )
                        )
                        if expiry.action is LifecycleAction.AWAIT_INPUT:
                            continue
                        return "expired"
                    continue
                waiting = False
            task = asyncio.create_task(run_investigation(investigation_agent, run_id))
            await workflow.wait_condition(partial(self._work_done, task))
            if self._is_cancelled():
                task.cancel()
                with suppress(asyncio.CancelledError, ActivityError, AgentRunError):
                    await task
                break
            try:
                result = await task
            except (ActivityError, AgentRunError) as error:
                failed = lifecycle.after_interruption(interruption(error))
                if failed.action is LifecycleAction.INVESTIGATE:
                    continue
                if failed.action is LifecycleAction.CANCEL:
                    self._cancelled = True
                    break
                return await self._stop(run_id, failed)
            draft = proposal(run_id, sequence, result)
            if isinstance(draft, AnswerDraft):
                outcome = await workflow.execute_activity(
                    activities.release_answer, draft, **_OPTIONS
                )
            else:
                outcome = await workflow.execute_activity(
                    activities.ask, draft, **_OPTIONS
                )
            sequence += 1
            released = lifecycle.after_output(outcome)
            if released.action is LifecycleAction.CLOSE:
                return "closed"
            if released.action is LifecycleAction.CANCEL:
                self._cancelled = True
                break
            if released.action is LifecycleAction.STOP:
                return await self._stop(run_id, released)
            waiting = released.action is LifecycleAction.AWAIT_INPUT
            # A fresh agent run receives rebuilt context. Old model messages
            # cannot carry revoked evidence or superseded assumptions forward.
        progress = await workflow.execute_activity(
            activities.begin_cancel, run_id, **_OPTIONS
        )
        for _ in range(lifecycle.CANCEL_SETTLE_CHECKS):
            if lifecycle.cancellation_settled(progress):
                break
            await asyncio.sleep(lifecycle.CANCEL_SETTLE_INTERVAL.total_seconds())
            progress = await workflow.execute_activity(
                activities.reconcile_cancel, run_id, **_OPTIONS
            )
        await workflow.execute_activity(
            activities.finish_cancelled,
            activities.CancelRequest(run_id, lifecycle.cancellation_settled(progress)),
            **_OPTIONS,
        )
        return "cancelled"

    async def _stop(self, run_id: str, decision: LifecycleDecision) -> str:
        await workflow.execute_activity(
            activities.finish,
            FinishRequest(
                run_id,
                decision.stop_reason or StopReason.INTERRUPTED,
                decision.resource,
            ),
            **_OPTIONS,
        )
        return "stopped"
