"""The local in-process manager on real PostgreSQL, without any Temporal server.

The same controlled tool/model (``scripted_investigations``) as the Temporal
lifecycle tests drive the shared application decisions through the local
backend: answer, duplicate start, clarification without model polling,
steering, context restart, cancellation and queue order, budgets, provider
fallback, reconnect/replay, truthful shutdown, the one-manager lock and
ownership separation from Temporal runs.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy as sa
from pydantic_ai.models.function import FunctionModel

from retail_analytics.adapters.local.investigations import LocalInvestigationManager
from retail_analytics.adapters.postgres.database import Database
from retail_analytics.adapters.postgres.investigation_recovery import (
    PostgresRecoveryCandidates,
)
from retail_analytics.adapters.postgres.investigations import (
    PostgresInvestigationInputs,
    PostgresRunPrincipals,
)
from retail_analytics.adapters.postgres.local_execution import ManagerLockHeld
from retail_analytics.application.contracts.authorization import Principal
from retail_analytics.application.contracts.persistence import (
    IdempotencyConflict,
    RunRequest,
)
from retail_analytics.application.contracts.progress import EventKind
from retail_analytics.application.investigation_runtime import INTERRUPTED_NOTICE
from retail_analytics.application.investigations import RunNotActive
from retail_analytics.bootstrap.config import BackendSettings
from retail_analytics.bootstrap.local_investigations import (
    LocalInvestigations,
    build_local_investigations,
)
from retail_analytics.domain.investigations import (
    InputKind,
    InputStatus,
    RunInput,
    input_id_for,
)
from retail_analytics.domain.runs import ExecutionBackend, RunStatus, WorkflowRef
from tests.integration.compose_stack import Stack, running_stack
from tests.integration.scripted_investigations import (
    effect_registry,
    fallback_chain,
    scripted_model,
)
from tests.integration.test_context import Env

pytestmark = [pytest.mark.docker, pytest.mark.asyncio]


@pytest.fixture(scope="module")
def stack() -> Iterator[Stack]:
    with running_stack("postgres") as stack:
        stack.migrate()
        env = Env(stack)
        with env.db.engine.begin() as connection:
            connection.execute(
                sa.text(
                    "CREATE TABLE t13_test_effects ("
                    "operation_id text PRIMARY KEY, run_id text NOT NULL)"
                )
            )
        env.db.close()
        yield stack


class LocalEnv(Env):
    __test__ = False
    principal: Principal
    session_id: str

    def build(
        self, *, settings: BackendSettings | None = None, model: Any = None
    ) -> LocalInvestigations:
        settings = settings or BackendSettings()
        return build_local_investigations(
            settings,
            self.db,
            self.access,
            model or FunctionModel(scripted_model),
            registry=effect_registry(self.db),
            shutdown_grace=timedelta(seconds=20),
        )

    async def start(
        self,
        local: LocalInvestigations,
        text: str,
        *,
        session_id: str | None = None,
        submission_key: str | None = None,
        principal: Principal | None = None,
    ) -> str:
        handle = await local.services.control.start(
            principal or self.principal,
            session_id=session_id or self.session_id,
            text=text,
            submission_key=submission_key or uuid.uuid4().hex,
        )
        return handle.run_id

    async def wait_status(
        self, run_id: str, target: RunStatus, deadline_seconds: float = 60
    ) -> None:
        async with asyncio.timeout(deadline_seconds):
            while True:
                run = await self.db.runs.get_run(run_id)
                if run is not None and run.status is target:
                    return
                await asyncio.sleep(0.05)

    async def model_requests(self, run_id: str) -> int:
        budget = await self.db.budgets.get(run_id)
        assert budget is not None
        return budget.usage.provider_requests

    def scalar(self, sql: str, **params: Any) -> Any:
        with self.db.engine.connect() as connection:
            return connection.execute(sa.text(sql), params).scalar_one()

    def effects(self, run_id: str) -> int:
        return int(
            self.scalar(
                "SELECT count(*) FROM t13_test_effects WHERE run_id = :run", run=run_id
            )
        )

    async def assistant_texts(self, session_id: str | None = None) -> list[str]:
        messages = await self.db.sessions.recent_messages(
            session_id or self.session_id, 200
        )
        return [m.content for m in messages if m.role.value == "assistant"]


@pytest_asyncio.fixture
async def env(stack: Stack) -> AsyncIterator[LocalEnv]:
    env = LocalEnv(stack)
    env.principal, env.session_id = await env.executive({"1"})
    try:
        yield env
    finally:
        env.db.close()


@pytest_asyncio.fixture
async def local(env: LocalEnv) -> AsyncIterator[LocalInvestigations]:
    built = env.build()
    async with built.manager:
        yield built


async def test_answer_runs_once_and_duplicate_start_is_one_execution(
    env: LocalEnv, local: LocalInvestigations
) -> None:
    key = uuid.uuid4().hex
    text = "Analyze sales: effect case."
    # Concurrent duplicate submissions converge on one run and one task.
    first, second = await asyncio.gather(
        env.start(local, text, submission_key=key),
        env.start(local, text, submission_key=key),
    )
    assert first == second
    assert local.manager.running() <= {first}
    await env.wait_status(first, RunStatus.COMPLETED)
    # Starting the persisted run again (resubmission, recovery, a direct
    # scheduler call) never executes it a second time.
    again = await local.services.control.start(
        env.principal, session_id=env.session_id, text=text, submission_key=key
    )
    assert again.run_id == first and not again.created
    reference = await local.manager.start(first)
    assert reference.backend is ExecutionBackend.LOCAL
    await asyncio.sleep(0.5)
    assert local.manager.running() == frozenset()
    assert env.effects(first) == 1
    assert len(await env.db.tool_executions.for_run(first)) == 1
    run = await env.db.runs.get_run(first)
    assert run is not None and run.execution_backend is ExecutionBackend.LOCAL
    assert run.workflow == WorkflowRef(
        local.manager.instance_id, None, ExecutionBackend.LOCAL
    )
    # No fake Temporal identifiers are stamped on a local run.
    assert (
        env.scalar(
            "SELECT count(*) FROM runs WHERE run_id = :r AND "
            "temporal_workflow_id IS NULL AND temporal_run_id IS NULL "
            "AND execution_backend = 'local'",
            r=first,
        )
        == 1
    )
    assert any("investigation is complete" in t for t in await env.assistant_texts())


async def test_clarification_waits_without_model_calls_then_resumes(
    env: LocalEnv, local: LocalInvestigations
) -> None:
    run_id = await env.start(local, "Analyze sales: clarification case.")
    await env.wait_status(run_id, RunStatus.WAITING_FOR_INPUT)
    attached = await local.services.control.attach(env.principal, run_id=run_id)
    assert attached.open_question_id is not None
    before = await env.model_requests(run_id)
    await asyncio.sleep(2)
    assert await env.model_requests(run_id) == before  # no polling the model
    assert local.manager.running() == {run_id}  # the task waits in-process
    await local.services.control.answer(
        env.principal,
        run_id=run_id,
        question_id=attached.open_question_id,
        text="Use last full month sales.",
        submission_key="answer",
    )
    await env.wait_status(run_id, RunStatus.COMPLETED)
    assert await env.model_requests(run_id) > before


async def test_steering_supersedes_inflight_answer(
    env: LocalEnv, local: LocalInvestigations
) -> None:
    run_id = await env.start(local, "Analyze sales: slow case.")
    async with asyncio.timeout(30):
        while True:
            if await env.db.budgets.get(run_id) is not None:
                break
            await asyncio.sleep(0.05)
    await asyncio.sleep(1)
    await local.services.control.steer(
        env.principal,
        run_id=run_id,
        text="Instead use annual sales.",
        submission_key="steer",
    )
    await env.wait_status(run_id, RunStatus.COMPLETED)
    texts = await env.assistant_texts()
    assert any("updated annual" in t for t in texts)
    assert "The requested sales investigation is complete." not in texts


async def test_stale_context_restarts_without_repeating_the_effect(
    env: LocalEnv, local: LocalInvestigations, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("T13_EFFECT_DELAY", "2")
    run_id = await env.start(local, "Analyze sales: effect case.")
    async with asyncio.timeout(30):
        while True:
            if env.effects(run_id):
                break
            await asyncio.sleep(0.05)
    # Arrives while the tool runs: the next model request sees changed
    # context and the agent loop restarts with rebuilt context.
    await local.services.control.steer(
        env.principal,
        run_id=run_id,
        text="Then use annual sales.",
        submission_key="context-change",
    )
    await env.wait_status(run_id, RunStatus.COMPLETED)
    assert any("updated annual" in t for t in await env.assistant_texts())
    assert env.effects(run_id) == 1
    assert len(await env.db.tool_executions.for_run(run_id)) == 1


async def test_cancel_while_waiting_then_queue_runs_in_order(
    env: LocalEnv, local: LocalInvestigations
) -> None:
    control = local.services.control
    run_id = await env.start(local, "Analyze sales: clarification case.")
    await env.wait_status(run_id, RunStatus.WAITING_FOR_INPUT)
    first = await control.enqueue(
        env.principal,
        session_id=env.session_id,
        text="Analyze product sales first.",
        submission_key="queue-1",
    )
    second = await control.enqueue(
        env.principal,
        session_id=env.session_id,
        text="Analyze product sales second.",
        submission_key="queue-2",
    )
    assert first.run_id is None and second.run_id is None
    await control.cancel(env.principal, run_id=run_id)
    await env.wait_status(run_id, RunStatus.CANCELLED)
    async with asyncio.timeout(60):
        while True:
            with env.db.engine.connect() as connection:
                rows = connection.execute(
                    sa.text(
                        "SELECT status, promoted_run_id FROM run_inputs "
                        "WHERE input_id IN (:a, :b) ORDER BY position"
                    ),
                    {"a": first.input_id, "b": second.input_id},
                ).all()
            if len(rows) == 2 and all(
                row.status == InputStatus.PROMOTED.value for row in rows
            ):
                break
            await asyncio.sleep(0.05)
    await env.wait_status(rows[1].promoted_run_id, RunStatus.COMPLETED)
    one = await env.db.runs.get_run(rows[0].promoted_run_id)
    two = await env.db.runs.get_run(rows[1].promoted_run_id)
    assert one and two and one.completed_at and one.completed_at <= two.created_at
    assert one.execution_backend is two.execution_backend is ExecutionBackend.LOCAL
    with pytest.raises(RunNotActive):
        await control.steer(
            env.principal, run_id=run_id, text="Too late.", submission_key="late"
        )


async def test_reconnect_replays_events_and_never_restarts_work(
    env: LocalEnv, local: LocalInvestigations
) -> None:
    control = local.services.control
    run_id = await env.start(local, "Analyze sales: slow case.")
    async with asyncio.timeout(30):
        while True:
            early = await control.attach(env.principal, run_id=run_id)
            if early.events:
                break
            await asyncio.sleep(0.05)
    cursor = early.events[-1].event_id
    # The client goes away; the run continues without it.
    await env.wait_status(run_id, RunStatus.COMPLETED)
    resumed = await control.attach(env.principal, run_id=run_id, after_event_id=cursor)
    assert resumed.run.status is RunStatus.COMPLETED
    assert cursor not in {e.event_id for e in resumed.events}
    assert EventKind.RUN_COMPLETED in {e.kind for e in resumed.events}
    full = await control.attach(env.principal, run_id=run_id)
    assert [e.event_id for e in full.events] == [
        *(e.event_id for e in early.events),
        *(e.event_id for e in resumed.events),
    ]
    # Attaching never schedules anything.
    assert local.manager.running() == frozenset()
    assert await env.model_requests(run_id) == 1


async def test_budget_stop_and_model_fallback_use_shared_policy(
    env: LocalEnv,
) -> None:
    limited = env.build(settings=BackendSettings(run_max_provider_requests=1))
    async with limited.manager:
        run_id = await env.start(limited, "Analyze sales: effect case.")
        async with asyncio.timeout(60):
            while True:
                run = await env.db.runs.get_run(run_id)
                if run is not None and run.status.is_terminal:
                    break
                await asyncio.sleep(0.05)
        assert run.status in (RunStatus.PARTIAL, RunStatus.FAILED)
        assert await env.model_requests(run_id) == 1
        assert env.effects(run_id) == 1
    fallback = env.build(model=fallback_chain(BackendSettings()))
    async with fallback.manager:
        run_id = await env.start(fallback, "Analyze sales: effect case.")
        await env.wait_status(run_id, RunStatus.COMPLETED, 90)
        assert env.effects(run_id) == 1  # the backup did not redo the effect
        assert any(
            "Backup continued after g-1." in t for t in await env.assistant_texts()
        )
        # Gemini call + 3 overloaded Gemini attempts + GPT answer.
        assert await env.model_requests(run_id) == 5


async def test_shutdown_records_truthful_interruption_and_discards_queue(
    env: LocalEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("T13_EFFECT_DELAY", "30")
    local = env.build()
    await local.manager.open()
    try:
        control = local.services.control
        busy = await env.start(local, "Analyze sales: effect case.")
        queued = await control.enqueue(
            env.principal,
            session_id=env.session_id,
            text="Analyze product sales next.",
            submission_key="queued",
        )
        other, other_session = await env.executive({"1"})
        waiting = await env.start(
            local,
            "Analyze sales: clarification case.",
            session_id=other_session,
            principal=other,
        )
        await env.wait_status(waiting, RunStatus.WAITING_FOR_INPUT)
        async with asyncio.timeout(30):
            while True:
                if env.effects(busy):
                    break
                await asyncio.sleep(0.05)
        requests = await env.model_requests(busy)
    finally:
        await local.manager.close()

    for run_id in (busy, waiting):
        run = await env.db.runs.get_run(run_id)
        assert run is not None and run.status is RunStatus.FAILED
    texts = await env.assistant_texts()
    assert any(
        INTERRUPTED_NOTICE in t and "queued request was not started" in t for t in texts
    )
    assert await local.services.inputs.open_question(waiting) is None
    status = env.scalar(
        "SELECT status FROM run_inputs WHERE input_id = :i", i=queued.input_id
    )
    assert status == InputStatus.DISCARDED.value  # kept as history, not run
    assert await env.db.runs.active_run(env.session_id) is None
    assert await env.model_requests(busy) == requests
    assert env.effects(busy) == 1
    events = await env.db.run_events.replay(busy, limit=1000)
    assert events[-1].kind is EventKind.RUN_FAILED
    # The lock was released: a new manager starts, and the session accepts a
    # new explicit request (nothing old resumes).
    monkeypatch.setenv("T13_EFFECT_DELAY", "0")
    restarted = env.build()
    async with restarted.manager as manager:
        assert manager.running() == frozenset()
        fresh = await env.start(restarted, "Analyze sales for last month.")
        await env.wait_status(fresh, RunStatus.COMPLETED)
    assert env.effects(busy) == 1


async def test_second_local_manager_is_rejected(env: LocalEnv) -> None:
    first = env.build()
    second = env.build()
    async with first.manager:
        with pytest.raises(ManagerLockHeld):
            await second.manager.open()
        assert not second.manager.admitting
    async with second.manager:
        assert second.manager.admitting


async def _owned_run(
    env: LocalEnv, backend: ExecutionBackend, reference: WorkflowRef
) -> tuple[str, str, str]:
    """An active run of ``backend`` with a queued request behind it."""
    principal, session_id = await env.executive({"1"})
    run_id = f"run-{backend.value}-" + uuid.uuid4().hex
    await env.db.runs.start_run(
        RunRequest(
            run_id=run_id,
            session_id=session_id,
            requested_by=principal.executive_id,
            submission_key="owned",
            message_id="msg-" + run_id,
            request_text=f"A {backend.value} investigation.",
            execution_backend=backend,
        )
    )
    await env.db.runs.attach_workflow(run_id, reference)
    await PostgresRunPrincipals(Database(env.db.engine)).record(run_id, principal)
    queued_id = input_id_for(session_id, "queued")
    await PostgresInvestigationInputs(Database(env.db.engine)).add(
        RunInput(
            input_id=queued_id,
            session_id=session_id,
            kind=InputKind.QUEUED,
            content="Queued behind the run.",
            status=InputStatus.PENDING,
            created_at=datetime.now(UTC),
        )
    )
    return run_id, session_id, queued_id


async def test_local_manager_never_touches_temporal_runs(env: LocalEnv) -> None:
    temporal = WorkflowRef("investigation/t-" + uuid.uuid4().hex, "wf-run")
    temporal_run, temporal_session, temporal_queued = await _owned_run(
        env, ExecutionBackend.TEMPORAL, temporal
    )
    # A local run whose manager instance died mid-run.
    dead = WorkflowRef("local-dead-instance", None, ExecutionBackend.LOCAL)
    local_run, local_session, local_queued = await _owned_run(
        env, ExecutionBackend.LOCAL, dead
    )
    # The Temporal recovery dispatcher ignores local runs and their queues.
    candidates = PostgresRecoveryCandidates(Database(env.db.engine))
    active = {c.run_id for c in await candidates.active()}
    assert temporal_run in active and local_run not in active
    queued_sessions = set(await candidates.queued_sessions())
    assert temporal_session in queued_sessions
    assert local_session not in queued_sessions

    local = env.build()
    async with local.manager as manager:
        run = await env.db.runs.get_run(temporal_run)
        assert run is not None and run.status is RunStatus.RUNNING
        assert run.workflow == temporal
        assert (
            env.scalar(
                "SELECT status FROM run_inputs WHERE input_id = :i", i=temporal_queued
            )
            == InputStatus.PENDING.value
        )
        # Starting (taking over) a Temporal run is refused; nothing runs.
        with pytest.raises(IdempotencyConflict):
            await manager.start(temporal_run)
        assert manager.running() == frozenset()
        # The orphaned local run ended interrupted; its queue was discarded.
        swept = await env.db.runs.get_run(local_run)
        assert swept is not None and swept.status is RunStatus.FAILED
        assert swept.workflow == dead  # history keeps the dead owner
        assert (
            env.scalar(
                "SELECT status FROM run_inputs WHERE input_id = :i", i=local_queued
            )
            == InputStatus.DISCARDED.value
        )
        texts = await env.assistant_texts(local_session)
        assert any(
            INTERRUPTED_NOTICE in t and "queued request was not started" in t
            for t in texts
        )
        # The session accepts a new explicit request.
        assert await env.db.runs.active_run(local_session) is None


async def test_unopened_manager_refuses_to_execute(env: LocalEnv) -> None:
    local = env.build()
    run_id = await env.start(local, "Analyze sales for last month.")
    run = await env.db.runs.get_run(run_id)
    # Never admitted: ended truthfully at once instead of left waiting.
    assert run is not None and run.status is RunStatus.FAILED
    assert isinstance(local.manager, LocalInvestigationManager)
    assert local.manager.running() == frozenset()
    assert env.effects(run_id) == 0
