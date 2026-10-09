"""The stopped-run answer through the real runtime and PostgreSQL (T39-F2).

``InvestigationRuntime.finish_partial`` shows only results that bear on the
request, names the exhausted budget resource, makes no model call and keeps
the output gate: evidence that became unreadable withholds the findings.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from pydantic_ai.models.function import FunctionModel

from retail_analytics.application.contracts.investigations import (
    FinishRequest,
    StepResult,
    StopReason,
)
from retail_analytics.application.contracts.persistence import OperationRequest
from retail_analytics.application.contracts.progress import EventKind
from retail_analytics.application.contracts.telemetry import Metric, Span
from retail_analytics.application.contracts.tools import OperationContext
from retail_analytics.application.investigation_runtime import TRUNCATED_NOTE
from retail_analytics.application.telemetry import use_telemetry
from retail_analytics.bootstrap.config import BackendSettings, RuntimeMode
from retail_analytics.bootstrap.local_investigations import (
    build_local_investigations,
)
from retail_analytics.domain.budgets import BudgetResource
from retail_analytics.domain.operations import SideEffect
from retail_analytics.domain.runs import RunStatus
from tests.integration.compose_stack import Stack, running_stack
from tests.integration.scripted_investigations import scripted_model
from tests.integration.test_security_release_gates import ReleaseEnv
from tests.unit.evidence.support import basis, compiled_and_released
from tests.unit.telemetry.recording import recording

pytestmark = [pytest.mark.docker, pytest.mark.asyncio]

UNROUNDED = re.compile(r"\d+\.\d{3,}")


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


async def _stop(env: ReleaseEnv, run_id: str) -> tuple[RunStatus, str]:
    runtime = env.local.services.runtime
    await runtime.finish_partial(
        FinishRequest(run_id, StopReason.BUDGET, BudgetResource.TOKENS)
    )
    run = await env.db.runs.get_run(run_id)
    assert run is not None
    texts = await env.assistant_texts()
    return run.status, texts[-1]


async def _provider_requests(env: ReleaseEnv, run_id: str) -> int:
    budget = await env.db.budgets.get(run_id)
    assert budget is not None
    return budget.usage.provider_requests


async def test_relevant_result_is_shown_with_the_token_stop(env: ReleaseEnv) -> None:
    run_id = await env.run(env.principal, env.session_id, "Revenue for September?")
    evidence_id, _ = await env.query(env.principal, run_id)
    status, text = await _stop(env, run_id)
    assert status is RunStatus.PARTIAL
    assert "model token budget" in text
    assert "2026-09-01 to 2026-09-30 inclusive (UTC)" in text
    assert "Relevant verified results so far" in text
    assert text.rstrip().endswith(f"Evidence: {evidence_id}")
    assert not UNROUNDED.search(text)
    assert "cut off" not in text  # a token stop is not truncation
    # The fallback made no model request.
    assert await _provider_requests(env, run_id) == 0
    events = await env.db.run_events.replay(run_id, limit=100)
    partial = [e for e in events if e.kind is EventKind.RUN_PARTIAL]
    assert len(partial) == 1 and "model token budget" in partial[0].summary


async def test_earlier_unrelated_result_linked_to_the_run_is_not_shown(
    env: ReleaseEnv,
) -> None:
    earlier = await env.run(env.principal, env.session_id, "Customer overview")
    old_id = await _record(env, earlier, period=None)
    await env.local.services.runtime.finish_message(earlier, "Done.")
    run_id = await env.run(env.principal, env.session_id, "Revenue for September?")
    # Context building links every record the model was shown to the run.
    ctx = await env.access.resolver.context_for_run(env.principal, run_id)
    await env.evidence.link_to_run(ctx, [old_id])
    status, text = await _stop(env, run_id)
    assert status is RunStatus.FAILED
    assert "No verified result matches the period and measure" in text
    assert "1 other result" in text
    assert old_id not in text
    assert not re.search(r"\d{3,}", text.split("\n\n", 1)[1])


async def test_revoked_evidence_withholds_the_findings(env: ReleaseEnv) -> None:
    run_id = await env.run(env.principal, env.session_id, "Revenue for September?")
    evidence_id, refs = await env.query(env.principal, run_id)
    await env.db.access_admin.replace_products(env.principal.executive_id, {"2"})
    status, text = await _stop(env, run_id)
    assert status is RunStatus.FAILED
    assert "model token budget" in text
    assert "No verified findings can be shown." in text
    assert evidence_id not in text
    assert not any(ref in text for ref in refs)


async def test_no_evidence_says_so(env: ReleaseEnv) -> None:
    run_id = await env.run(env.principal, env.session_id, "Revenue for September?")
    status, text = await _stop(env, run_id)
    assert status is RunStatus.FAILED
    assert "No verified results were produced yet." in text
    assert await _provider_requests(env, run_id) == 0


async def test_token_budget_stop_through_the_local_manager(env: ReleaseEnv) -> None:
    limited = build_local_investigations(
        BackendSettings(mode=RuntimeMode.FIXTURE, run_max_tokens=1000),
        env.db,
        env.access,
        FunctionModel(scripted_model),
    )
    async with limited.manager:
        handle = await limited.services.control.start(
            env.principal,
            session_id=env.session_id,
            text="Analyze sales: effect case.",
            submission_key="t39f2-tokens",
        )
        async with asyncio.timeout(60):
            while True:
                run = await env.db.runs.get_run(handle.run_id)
                if run is not None and run.status.is_terminal:
                    break
                await asyncio.sleep(0.05)
    texts = await env.assistant_texts()
    assert "model token budget" in texts[-1]
    assert "this kind of work" not in texts[-1]
    # The refused reservation never reached the provider.
    assert await _provider_requests(env, handle.run_id) == 0


async def _deadline(env: ReleaseEnv, run_id: str) -> tuple[RunStatus, str]:
    runtime = env.local.services.runtime
    await runtime.finish_partial(
        FinishRequest(run_id, StopReason.BUDGET, BudgetResource.ACTIVE_TIME)
    )
    run = await env.db.runs.get_run(run_id)
    assert run is not None
    return run.status, (await env.assistant_texts())[-1]


async def test_deadline_shows_verified_findings_and_is_traced_once(
    env: ReleaseEnv,
) -> None:
    run_id = await env.run(env.principal, env.session_id, "Revenue for September?")
    evidence_id, _ = await env.query(env.principal, run_id)
    telemetry, sink = recording()
    with use_telemetry(telemetry):
        status, text = await _deadline(env, run_id)
        # A retried or replayed finish changes and counts nothing.
        await _deadline(env, run_id)
    assert status is RunStatus.PARTIAL
    assert text.startswith(
        "This investigation ran out of time: it reached its 2-minute limit "
        "for active work, so it stopped before finishing."
    )
    assert "Relevant verified results so far (not a complete answer)" in text
    assert "Not answered:" in text
    assert text.rstrip().endswith(f"Evidence: {evidence_id}")
    assert "cut off" not in text and "truncat" not in text
    assert await _provider_requests(env, run_id) == 0  # no model call
    events = await env.db.run_events.replay(run_id, limit=100)
    partial = [e for e in events if e.kind is EventKind.RUN_PARTIAL]
    assert len(partial) == 1 and "ran out of time" in partial[0].summary
    assert sink.total(Metric.BUDGET_STOPS, resource="active_time") == 1
    assert sink.total(Metric.BUDGET_STOPS) == 1
    (root,) = sink.named(Span.RUN)
    attributes = root.attributes
    assert attributes["deadline_stop"] is True
    assert attributes["stop_reason"] == "budget"
    assert attributes["stop_resource"] == "active_time"
    assert attributes["status"] == "partial"
    assert attributes["active_limit_seconds"] == 120
    assert attributes["verified_results"] == 1
    assert isinstance(attributes["active_seconds"], float)
    assert isinstance(attributes["elapsed_seconds"], float)
    active = [m for m, _, _ in sink.observations if m is Metric.RUN_ACTIVE_SECONDS]
    assert len(active) == 1


async def test_deadline_without_usable_evidence_says_so(env: ReleaseEnv) -> None:
    run_id = await env.run(env.principal, env.session_id, "Revenue for September?")
    status, text = await _deadline(env, run_id)
    assert status is RunStatus.FAILED
    assert "ran out of time" in text
    assert "No verified results were produced yet." in text
    assert "Your question was not answered." in text


async def test_answer_accepted_before_the_deadline_stays_complete(
    env: ReleaseEnv,
) -> None:
    run_id = await env.run(env.principal, env.session_id, "Revenue for September?")
    evidence_id, _ = await env.query(env.principal, run_id)
    assert (
        await env.answer(run_id, "September revenue is in the result.", evidence_id)
        is StepResult.RELEASED
    )
    telemetry, sink = recording()
    with use_telemetry(telemetry):
        status, text = await _deadline(env, run_id)
    assert status is RunStatus.COMPLETED
    assert "ran out of time" not in text
    assert sink.total(Metric.BUDGET_STOPS) == 0


async def test_truncated_result_is_not_a_time_stop_and_vice_versa(
    env: ReleaseEnv,
) -> None:
    run_id = await env.run(env.principal, env.session_id, "Revenue for September?")
    evidence_id = await _record(env, run_id, max_rows=1)
    assert (
        await env.answer(run_id, "September revenue is in the result.", evidence_id)
        is StepResult.RELEASED
    )
    run = await env.db.runs.get_run(run_id)
    assert run is not None and run.status is RunStatus.PARTIAL
    text = (await env.assistant_texts())[-1]
    assert TRUNCATED_NOTE in text and "ran out of time" not in text
    # A time stop on another run never claims truncation.
    other = await env.run(env.principal, env.session_id, "Revenue for September?")
    await env.query(env.principal, other)
    _, stopped = await _deadline(env, other)
    assert "ran out of time" in stopped and TRUNCATED_NOTE not in stopped


async def _record(
    env: ReleaseEnv, run_id: str, max_rows: int | None = None, **overrides: object
) -> str:
    ctx = await env.access.resolver.context_for_run(env.principal, run_id)
    op = await env.db.tool_executions.begin(
        OperationRequest(
            operation_id=f"op-{run_id}",
            run_id=run_id,
            capability="execute_analysis",
            capability_version=1,
            side_effect=SideEffect.EXTERNAL_JOB,
        )
    )
    compiled, released = compiled_and_released(
        ctx.executive_id, ctx.product_scope, max_rows=max_rows
    )
    evidence = await env.evidence.record_query(
        OperationContext(ctx, op.execution.operation_id),
        compiled,
        released,
        basis(**overrides),
    )
    return evidence.evidence_id
