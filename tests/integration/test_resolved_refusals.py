"""A fully resolved refusal completes the run, through the real runtime and
PostgreSQL (T26-F10).

``release_answer`` marks a run COMPLETED when every part of the request was
answered or declined under a restriction the application confirms, and keeps
it PARTIAL while permitted work remains or a restriction is only claimed.
Budget stops still end PARTIAL.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from pydantic_ai.models.function import FunctionModel

from retail_analytics.application.contracts.investigations import (
    AnswerDraft,
    FinishRequest,
    Restriction,
    StepResult,
    StopReason,
)
from retail_analytics.application.contracts.persistence import OperationRequest
from retail_analytics.application.contracts.progress import EventKind
from retail_analytics.application.query_execution import SCOPE_REFUSAL
from retail_analytics.bootstrap.config import BackendSettings, RuntimeMode
from retail_analytics.bootstrap.local_investigations import (
    build_local_investigations,
)
from retail_analytics.domain.budgets import BudgetResource
from retail_analytics.domain.executions import ToolExecutionStatus
from retail_analytics.domain.operations import SideEffect, ToolErrorCode
from retail_analytics.domain.runs import RunStatus
from tests.integration.compose_stack import Stack, running_stack
from tests.integration.scripted_investigations import scripted_model
from tests.integration.test_security_release_gates import ReleaseEnv

pytestmark = [pytest.mark.docker, pytest.mark.asyncio]

REFUSAL = (
    "Customer demographics such as age band cannot be provided for an "
    "individual customer, including the single biggest spender: demographics "
    "are available only as group-level statistics. As an alternative, the age "
    "band with the highest total spend in Q4 2025 was 65-69."
)


@pytest.fixture(scope="module")
def stack() -> Iterator[Stack]:
    with running_stack("postgres") as stack:
        stack.migrate()
        yield stack


@pytest_asyncio.fixture
async def env(stack: Stack) -> AsyncIterator[ReleaseEnv]:
    env = ReleaseEnv(stack)
    env.local = build_local_investigations(
        BackendSettings(mode=RuntimeMode.FIXTURE),
        env.db,
        env.access,
        FunctionModel(scripted_model),
    )
    env.principal, env.session_id = await env.executive({"1"})
    try:
        yield env
    finally:
        env.db.close()


async def _release(
    env: ReleaseEnv,
    text: str,
    *,
    complete: bool,
    declined: tuple[Restriction, ...],
    request: str = "Which age band is our single biggest-spending customer?",
    scope_refusal: bool = False,
) -> tuple[RunStatus, str, list[EventKind]]:
    run_id = await env.run(env.principal, env.session_id, request)
    if scope_refusal:
        # What the query service records for a brand outside the scope.
        op = await env.db.tool_executions.begin(
            OperationRequest(
                operation_id="op-" + uuid.uuid4().hex[:12],
                run_id=run_id,
                capability="execute_analysis",
                capability_version=1,
                side_effect=SideEffect.EXTERNAL_JOB,
            )
        )
        await env.db.tool_executions.transition(
            op.execution.operation_id,
            ToolExecutionStatus.FAILED,
            attempt=1,
            error_code=ToolErrorCode.ACCESS_DENIED,
            detail=SCOPE_REFUSAL,
        )
    outcome = await env.local.services.runtime.release_answer(
        AnswerDraft(run_id, 1, text, complete=complete, declined=declined)
    )
    assert outcome.result is StepResult.RELEASED
    run = await env.db.runs.get_run(run_id)
    assert run is not None
    events = await env.db.run_events.replay(run_id, limit=100)
    return run.status, (await env.assistant_texts())[-1], [e.kind for e in events]


async def test_refusal_only_request_completes_with_restriction_and_alternative(
    env: ReleaseEnv,
) -> None:
    status, text, kinds = await _release(
        env,
        REFUSAL,
        complete=True,
        declined=(Restriction.INDIVIDUAL_DEMOGRAPHICS,),
    )
    assert status is RunStatus.COMPLETED
    assert "group-level statistics" in text  # the restriction
    assert "As an alternative" in text  # the alternative
    assert EventKind.RUN_COMPLETED in kinds
    assert EventKind.RUN_PARTIAL not in kinds


async def test_mixed_request_completes_when_permitted_work_is_done(
    env: ReleaseEnv,
) -> None:
    status, _, _ = await _release(
        env,
        REFUSAL + " Total Q4 revenue for all age bands is shown above.",
        complete=True,
        declined=(Restriction.INDIVIDUAL_DEMOGRAPHICS,),
        request="Q4 revenue by age band, and the top customer's age band?",
    )
    assert status is RunStatus.COMPLETED


async def test_mixed_request_stays_partial_while_permitted_work_remains(
    env: ReleaseEnv,
) -> None:
    status, _, kinds = await _release(
        env,
        REFUSAL + " The monthly breakdown is still open.",
        complete=False,
        declined=(Restriction.INDIVIDUAL_DEMOGRAPHICS,),
        request="Monthly revenue by age band, and the top customer's age band?",
    )
    assert status is RunStatus.PARTIAL
    assert EventKind.RUN_PARTIAL in kinds


async def test_scope_refusal_needs_the_recorded_refusal(env: ReleaseEnv) -> None:
    text = "Brand Beta is outside your permitted scope; your brands are Alpha."
    claimed, _, _ = await _release(
        env,
        text,
        complete=True,
        declined=(Restriction.OUTSIDE_PERMITTED_SCOPE,),
        request="Revenue for brand Beta?",
    )
    assert claimed is RunStatus.PARTIAL
    recorded, _, _ = await _release(
        env,
        text,
        complete=True,
        declined=(Restriction.OUTSIDE_PERMITTED_SCOPE,),
        request="Revenue for brand Beta?",
        scope_refusal=True,
    )
    assert recorded is RunStatus.COMPLETED


async def test_budget_stop_still_ends_partial(env: ReleaseEnv) -> None:
    run_id = await env.run(env.principal, env.session_id, "Revenue for September?")
    await env.query(env.principal, run_id)
    await env.local.services.runtime.finish_partial(
        FinishRequest(run_id, StopReason.BUDGET, BudgetResource.TOKENS)
    )
    run = await env.db.runs.get_run(run_id)
    assert run is not None and run.status is RunStatus.PARTIAL
