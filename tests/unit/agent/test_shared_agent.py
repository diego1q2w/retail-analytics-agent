"""The shared agent and the application's lifecycle policy, without Temporal.

A fake execution drives the same agent factory, guarded model and catalog
toolset the Temporal workflow uses, then asks the lifecycle policy what
follows: answer -> close, clarification -> wait for input, stale context ->
restart, stop -> partial findings.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from retail_analytics.adapters.agent.investigator import (
    AgentBinding,
    AgentUnbound,
    build_investigation_agent,
    proposal,
    run_investigation,
)
from retail_analytics.application import investigation_lifecycle as lifecycle
from retail_analytics.application.contracts.investigations import (
    AgentInterruption,
    AnswerDraft,
    InterruptionKind,
    LifecycleAction,
    QuestionDraft,
    StepOutcome,
    StepResult,
    StopReason,
)
from retail_analytics.application.investigation_runtime import (
    InvestigationContextChanged,
    RunStopped,
)
from retail_analytics.domain.budgets import BudgetResource
from tests.unit.agent.scenario import STEP, scenario

pytestmark = pytest.mark.asyncio


async def test_answer_runs_guarded_and_closes_the_run() -> None:
    run = scenario()
    result = await run_investigation(run.agent, "run-1")
    draft = proposal("run-1", 2, result)
    assert draft == AnswerDraft("run-1", 2, "Sales grew 4%.", ("e1",), True, None)
    # One tool call through the single tool path, with the model's call id.
    assert run.tools.calls == [("run-1", "c1", "lookup", {"metric": "sales"})]
    # Every model request was prepared under current authority, framed by
    # its instructions and offered only the step's permitted tools.
    assert run.steps.calls == 2
    assert run.provider.instructions(0) == [STEP.instructions]
    assert run.provider.offered_tools == [{"lookup"}, {"lookup"}]
    released = StepOutcome(StepResult.RELEASED)
    assert lifecycle.after_output(released).action is LifecycleAction.CLOSE


async def test_clarification_waits_for_input() -> None:
    run = scenario(ends_with="clarify")
    draft = proposal("run-1", 0, await run_investigation(run.agent, "run-1"))
    assert draft == QuestionDraft("run-1", 0, "Which sales period should I use?")
    asked = StepOutcome(StepResult.ASKED, question_id="q1")
    assert lifecycle.after_output(asked).action is LifecycleAction.AWAIT_INPUT
    idle = lifecycle.after_resume(StepOutcome(StepResult.IDLE))
    assert idle.action is LifecycleAction.WAIT


async def test_stale_context_restarts_without_reaching_the_provider() -> None:
    run = scenario(STEP, replace(STEP, history_key="scope-v2"))
    with pytest.raises(InvestigationContextChanged):
        await run_investigation(run.agent, "run-1")
    # The tool ran under the old context; the conversation built on it never
    # reached the provider again.
    assert len(run.provider.requests) == 1
    decision = lifecycle.after_interruption(
        AgentInterruption(InterruptionKind.CONTEXT_CHANGED)
    )
    assert decision.action is LifecycleAction.INVESTIGATE
    fresh = scenario(replace(STEP, history_key="scope-v2"))
    draft = proposal("run-1", 0, await run_investigation(fresh.agent, "run-1"))
    assert isinstance(draft, AnswerDraft)


async def test_run_stopped_is_raised_as_is_and_ends_with_partial_findings() -> None:
    run = scenario()

    async def spent(run_id: str) -> None:
        raise RunStopped(StopReason.BUDGET, BudgetResource.TOKENS)

    run.steps.prepare_model_step = spent  # type: ignore[assignment,method-assign]
    with pytest.raises(RunStopped) as stopped:
        await run_investigation(run.agent, "run-1")
    assert run.provider.requests == []
    decision = lifecycle.after_interruption(
        AgentInterruption(
            InterruptionKind.STOPPED, stopped.value.reason, stopped.value.resource
        )
    )
    assert decision.action is LifecycleAction.STOP
    assert (decision.stop_reason, decision.resource) == (
        StopReason.BUDGET,
        BudgetResource.TOKENS,
    )


async def test_unbound_agent_fails_closed() -> None:
    with pytest.raises(AgentUnbound):
        await run_investigation(build_investigation_agent(AgentBinding()), "run-1")
