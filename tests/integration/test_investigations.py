"""Real PostgreSQL/Temporal lifecycle, process death and replay gates."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from pydantic import JsonValue
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from pydantic_ai.models.function import FunctionModel
from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest
from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Replayer

from retail_analytics.adapters.postgres.database import Database
from retail_analytics.adapters.postgres.investigation_recovery import (
    PostgresRecoveryCandidates,
)
from retail_analytics.adapters.temporal.scheduler import TemporalInvestigationScheduler
from retail_analytics.adapters.temporal.workflow import InvestigationWorkflow
from retail_analytics.application.authorization import Principal
from retail_analytics.application.investigation_recovery import InvestigationRecovery
from retail_analytics.application.investigation_runtime import (
    AnswerDraft,
    QuestionDraft,
    RunStopped,
    StepResult,
    StopReason,
)
from retail_analytics.application.investigations import RunNotActive
from retail_analytics.application.persistence import OperationRequest
from retail_analytics.application.query_execution import QueryCancelled
from retail_analytics.application.tools import ToolFailed
from retail_analytics.bootstrap.config import BackendSettings
from retail_analytics.bootstrap.investigations import (
    InvestigationServices,
    build_investigations,
)
from retail_analytics.domain.executions import ToolExecutionStatus
from retail_analytics.domain.investigations import InputStatus
from retail_analytics.domain.operations import SideEffect
from retail_analytics.domain.runs import RunStatus
from tests.integration.compose_stack import Stack, running_stack
from tests.integration.investigation_worker import effect_registry, scripted_model
from tests.integration.test_context import Env

pytestmark = [pytest.mark.docker, pytest.mark.asyncio]
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def stack() -> Iterator[Stack]:
    with running_stack("temporal") as stack:
        stack.compose("run", "--rm", "temporal-namespace")
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


class TestEnv(Env):
    __test__ = False
    client: Client
    queue: str
    scheduler: TemporalInvestigationScheduler
    services: InvestigationServices
    principal: Principal
    session_id: str

    @classmethod
    async def create(cls, stack: Stack) -> TestEnv:
        env = cls(stack)
        env.client = await Client.connect(
            f"127.0.0.1:{stack.temporal_port}", plugins=[PydanticAIPlugin()]
        )
        async with asyncio.timeout(30):
            while True:
                try:
                    await env.client.workflow_service.describe_namespace(
                        DescribeNamespaceRequest(namespace="default")
                    )
                    break
                except RPCError as error:
                    if error.status is not RPCStatusCode.NOT_FOUND:
                        raise
                    await asyncio.sleep(0.5)
        env.queue = "t13-" + uuid.uuid4().hex
        env.scheduler = TemporalInvestigationScheduler(env.client, env.queue)
        env.services = build_investigations(
            BackendSettings(),
            env.db,
            env.access,
            env.scheduler,
            FunctionModel(scripted_model),
            registry=effect_registry(env.db),
        )
        env.principal, env.session_id = await env.executive({"1"})
        return env

    def worker(self, stack: Stack, *, hold: bool = False) -> subprocess.Popen[str]:
        return subprocess.Popen(
            [sys.executable, "-m", "tests.integration.investigation_worker"],
            cwd=ROOT,
            env={
                **os.environ,
                "PYTHONPATH": str(ROOT / "src"),
                "T13_DATABASE_URL": stack.app_url,
                "T13_TEMPORAL_ADDRESS": f"127.0.0.1:{stack.temporal_port}",
                "T13_TASK_QUEUE": self.queue,
                "T13_CRASH_HOLD": "1" if hold else "0",
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    async def start(self, text: str = "Analyze sales for last month.") -> str:
        submission_key = uuid.uuid4().hex
        # Namespace registration can be visible to Describe before the
        # frontend cache used by workflow starts has refreshed.
        async with asyncio.timeout(30):
            while True:
                try:
                    handle = await self.services.control.start(
                        self.principal,
                        session_id=self.session_id,
                        text=text,
                        submission_key=submission_key,
                    )
                    return handle.run_id
                except RPCError as error:
                    if error.status is not RPCStatusCode.NOT_FOUND:
                        raise
                    await asyncio.sleep(0.5)

    async def wait_status(
        self, run_id: str, target: RunStatus, deadline_seconds: float = 60
    ) -> None:
        async with asyncio.timeout(deadline_seconds):
            while True:
                run = await self.db.runs.get_run(run_id)
                if run is not None and run.status is target:
                    return
                await asyncio.sleep(0.1)

    def stop(self, process: subprocess.Popen[str]) -> None:
        process.kill()
        process.communicate(timeout=10)

    async def model_requests(self, run_id: str) -> int:
        budget = await self.db.budgets.get(run_id)
        assert budget is not None
        return budget.usage.provider_requests


async def test_worker_kill_after_committed_effect_resumes_once_and_replays(
    stack: Stack,
) -> None:
    env = await TestEnv.create(stack)
    process = env.worker(stack, hold=True)
    replacement = None
    try:
        run_id = await env.start("Analyze sales: effect case.")
        async with asyncio.timeout(60):
            while True:
                with env.db.engine.connect() as connection:
                    count = connection.execute(
                        sa.text(
                            "SELECT count(*) FROM t13_test_effects WHERE run_id=:run"
                        ),
                        {"run": run_id},
                    ).scalar_one()
                if count == 1:
                    break
                if process.poll() is not None:
                    stdout, stderr = process.communicate()
                    pytest.fail(f"worker exited: {stdout} {stderr[-6000:]}")
                await asyncio.sleep(0.1)
        before = await env.model_requests(run_id)
        env.stop(process)
        replacement = env.worker(stack)
        await env.wait_status(run_id, RunStatus.COMPLETED, 90)
        operations = await env.db.tool_executions.for_run(run_id)
        assert len(operations) == 1
        with env.db.engine.connect() as connection:
            assert (
                connection.execute(
                    sa.text("SELECT count(*) FROM t13_test_effects WHERE run_id=:run"),
                    {"run": run_id},
                ).scalar_one()
                == 1
            )
        assert await env.model_requests(run_id) >= before
        handle = env.client.get_workflow_handle(env.scheduler.workflow_id(run_id))
        assert await handle.result() == "closed"
        history = await handle.fetch_history()
        await Replayer(
            workflows=[InvestigationWorkflow], plugins=[PydanticAIPlugin()]
        ).replay_workflow(history)
        payloads = tuple(
            data for event in history.events for data in _payload_bytes(event)
        )
        assert b"Analyze sales: effect case." not in b"\n".join(payloads)
        assert b"<evidence>" not in b"\n".join(payloads)
        assert max(map(len, payloads)) < 32768
        assert max(event.ByteSize() for event in history.events) < 65536
        print(
            f"replayed {len(history.events)} history events; "
            f"largest payload {max(map(len, payloads))} bytes"
        )
    finally:
        if process.poll() is None:
            env.stop(process)
        if replacement is not None:
            env.stop(replacement)
        env.db.close()


async def test_clarification_survives_restart_without_model_polling(
    stack: Stack,
) -> None:
    env = await TestEnv.create(stack)
    process = env.worker(stack)
    try:
        run_id = await env.start("Analyze sales: clarification case.")
        await env.wait_status(run_id, RunStatus.WAITING_FOR_INPUT)
        attached = await env.services.control.attach(env.principal, run_id=run_id)
        assert attached.open_question_id is not None
        before = await env.model_requests(run_id)
        env.stop(process)
        process = env.worker(stack)
        await asyncio.sleep(3)
        assert await env.model_requests(run_id) == before
        await env.services.control.answer(
            env.principal,
            run_id=run_id,
            question_id=attached.open_question_id,
            text="Use last full month sales.",
            submission_key="answer",
        )
        await env.wait_status(run_id, RunStatus.COMPLETED)
    finally:
        env.stop(process)
        env.db.close()


async def test_steering_supersedes_inflight_answer(stack: Stack) -> None:
    env = await TestEnv.create(stack)
    process = env.worker(stack)
    try:
        run_id = await env.start("Analyze sales: slow case.")
        async with asyncio.timeout(45):
            while True:
                if await env.db.budgets.get(run_id) is not None:
                    break
                await asyncio.sleep(0.1)
        await asyncio.sleep(1)
        await env.services.control.steer(
            env.principal,
            run_id=run_id,
            text="Instead use annual sales.",
            submission_key="steer",
        )
        await env.wait_status(run_id, RunStatus.COMPLETED)
        messages = await env.db.sessions.recent_messages(env.session_id, 100)
        assert any("updated annual" in message.content for message in messages)
        assert not any(
            message.content == "The requested sales investigation is complete."
            for message in messages
        )
    finally:
        env.stop(process)
        env.db.close()


async def test_cancel_while_waiting_and_queue_order(stack: Stack) -> None:
    env = await TestEnv.create(stack)
    process = env.worker(stack)
    try:
        run_id = await env.start("Analyze sales: clarification case.")
        await env.wait_status(run_id, RunStatus.WAITING_FOR_INPUT)
        first = await env.services.control.enqueue(
            env.principal,
            session_id=env.session_id,
            text="Analyze product sales first.",
            submission_key="queue-1",
        )
        second = await env.services.control.enqueue(
            env.principal,
            session_id=env.session_id,
            text="Analyze product sales second.",
            submission_key="queue-2",
        )
        assert first.run_id is None and second.run_id is None
        await env.services.control.cancel(env.principal, run_id=run_id)
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
                await asyncio.sleep(0.1)
        await env.wait_status(rows[1].promoted_run_id, RunStatus.COMPLETED)
        one = await env.db.runs.get_run(rows[0].promoted_run_id)
        two = await env.db.runs.get_run(rows[1].promoted_run_id)
        assert one and two and one.completed_at and one.completed_at <= two.created_at
        with pytest.raises(RunNotActive):
            await env.services.control.steer(
                env.principal,
                run_id=run_id,
                text="Use annual sales.",
                submission_key="too-late",
            )
    finally:
        env.stop(process)
        env.db.close()


async def test_clarification_retry_repairs_budget_pause(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = await TestEnv.create(stack)
    try:
        run_id = await env.start()
        runtime = env.services.runtime
        await runtime.begin(run_id)
        original = runtime._budgets.pause_for_clarification

        async def crash(run: str) -> Any:
            raise RuntimeError("controlled crash after WAITING commit")

        monkeypatch.setattr(runtime._budgets, "pause_for_clarification", crash)
        draft = QuestionDraft(run_id, 1, "Which sales period for jane@example.com?")
        with pytest.raises(RuntimeError, match="controlled crash"):
            await runtime.ask(draft)
        monkeypatch.setattr(runtime._budgets, "pause_for_clarification", original)
        assert (await runtime.ask(draft)).result is StepResult.ASKED
        budget = await env.db.budgets.get(run_id)
        assert budget and budget.usage.active_since is None
        attachment = await env.services.control.attach(env.principal, run_id=run_id)
        assert attachment.open_question_id is not None
        assert (await runtime.ask(draft)).result is StepResult.ASKED
        events = await runtime._events.replay(run_id, limit=100)
        questions = [event.input_request for event in events if event.input_request]
        assert len(questions) == 1
        assert "jane@example.com" not in questions[0].question
        await env.db.access_admin.set_active(env.principal.executive_id, False)
        denied = await runtime.ask(draft)
        assert denied.result is StepResult.STOPPED
        assert denied.stop_reason is StopReason.ACCESS
        assert await runtime._events.replay(run_id, limit=100) == events
    finally:
        env.db.close()


async def test_recovery_repairs_lost_notification_and_queued_promotion(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = await TestEnv.create(stack)
    try:
        run_id = await env.start()
        await env.services.runtime.begin(run_id)

        async def lost(run: str) -> None:
            raise RuntimeError("controlled notification loss")

        original = env.scheduler.notify_input
        monkeypatch.setattr(env.scheduler, "notify_input", lost)
        with pytest.raises(RuntimeError):
            await env.services.control.steer(
                env.principal,
                run_id=run_id,
                text="Use annual sales.",
                submission_key="saved-input",
            )
        assert len(await env.services.inputs.pending(run_id)) == 1
        monkeypatch.setattr(env.scheduler, "notify_input", original)
        recovery = InvestigationRecovery(
            PostgresRecoveryCandidates(Database(env.db.engine)),
            env.services.launcher,
            env.services.inputs,
            env.scheduler,
        )
        await recovery.dispatch()
        assert (
            await env.services.runtime.release_answer(
                AnswerDraft(run_id, 1, "Sales answer.")
            )
        ).result is StepResult.SUPERSEDED
    finally:
        env.db.close()


async def test_fresh_authority_at_model_tool_and_release_boundaries(
    stack: Stack,
) -> None:
    env = await TestEnv.create(stack)
    try:
        run_id = await env.start()
        await env.services.runtime.begin(run_id)
        await env.db.access_admin.set_active(env.principal.executive_id, False)
        with pytest.raises(RunStopped) as stopped:
            await env.services.runtime.prepare_model_step(run_id)
        assert stopped.value.reason is StopReason.ACCESS
        result = await env.services.tools.run(
            run_id, "blocked", "checkpoint_effect", {"purpose": "controlled"}
        )
        assert isinstance(result.outcome, ToolFailed)
        assert not await env.db.tool_executions.for_run(run_id)
        released = await env.services.runtime.release_answer(
            AnswerDraft(run_id, 1, "Sales answer.")
        )
        assert (
            released.result is StepResult.STOPPED
            and released.stop_reason is StopReason.ACCESS
        )
    finally:
        env.db.close()


async def test_release_masks_email_and_withholds_revoked_evidence(stack: Stack) -> None:
    env = await TestEnv.create(stack)
    try:
        run_id = await env.start()
        await env.services.runtime.begin(run_id)
        unsafe = await env.services.runtime.release_answer(
            AnswerDraft(run_id, 1, "Contact the buyer at buyer@example.test.")
        )
        assert unsafe.result is StepResult.RELEASED
        run_id = await env.start()
        await env.services.runtime.begin(run_id)
        evidence_id, _ = await env.query(env.principal, run_id)
        await env.db.access_admin.replace_products(env.principal.executive_id, {"2"})
        revoked = await env.services.runtime.release_answer(
            AnswerDraft(run_id, 2, "Sales answer.", (evidence_id,))
        )
        assert revoked.result in {StepResult.WITHHELD, StepResult.STOPPED}
        messages = await env.db.sessions.recent_messages(env.session_id, 100)
        assert not any("buyer@example.test" in message.content for message in messages)
    finally:
        env.db.close()


async def test_cancellation_keeps_uncertain_effect_pending_until_reconciled(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = await TestEnv.create(stack)
    try:
        run_id = await env.start()
        await env.services.runtime.begin(run_id)
        await env.db.tool_executions.begin(
            OperationRequest(
                operation_id="cancel-" + run_id,
                run_id=run_id,
                capability="execute_analysis",
                capability_version=1,
                side_effect=SideEffect.EXTERNAL_JOB,
            )
        )

        class Cancellation:
            async def cancel(self, run: str, operation_id: str) -> QueryCancelled:
                await env.db.tool_executions.transition(
                    operation_id, ToolExecutionStatus.CANCEL_REQUESTED, attempt=1
                )
                return QueryCancelled(None, False)

            async def reconcile_cancel(
                self, run: str, operation_id: str
            ) -> QueryCancelled:
                await env.db.tool_executions.transition(
                    operation_id, ToolExecutionStatus.CANCELLED, attempt=1
                )
                return QueryCancelled(None, True)

        monkeypatch.setattr(env.services.runtime, "_queries", Cancellation())
        assert (await env.services.runtime.begin_cancel(run_id)).unsettled == 1
        run = await env.db.runs.get_run(run_id)
        assert run and run.status is RunStatus.CANCELLING
        refused = await env.services.tools.run(
            run_id, "new", "checkpoint_effect", {"purpose": "new work"}
        )
        assert isinstance(refused.outcome, ToolFailed)
        assert (await env.services.runtime.reconcile_cancel(run_id)).unsettled == 0
        await env.services.runtime.finish_cancelled(run_id, settled=True)
        await env.wait_status(run_id, RunStatus.CANCELLED)
    finally:
        env.db.close()


def _payload_bytes(message: Any) -> Iterator[bytes]:
    if message.DESCRIPTOR.full_name == "temporal.api.common.v1.Payload":
        data = message.data
        assert isinstance(data, bytes)
        yield data
        return
    for descriptor, value in message.ListFields():
        if descriptor.type != descriptor.TYPE_MESSAGE:
            continue
        if descriptor.is_repeated:
            children = (
                value.values()
                if descriptor.message_type.GetOptions().map_entry
                else value
            )
            for child in children:
                yield from _payload_bytes(child)
        else:
            yield from _payload_bytes(value)


async def test_execute_analysis_reconciles_lost_response_and_records_one_evidence(
    stack: Stack,
) -> None:
    from retail_analytics.application.query_execution import (
        QueryAuthority,
        QueryExecutionService,
        QueryExecutionSettings,
    )
    from retail_analytics.application.result_privacy import ResultPrivacyBoundary
    from retail_analytics.application.tools import ToolSucceeded
    from retail_analytics.bootstrap.budgets import build_run_budgets
    from tests.unit.privacy.support import COMPILERS, customer_database
    from tests.unit.query_execution.fakes import FakeWarehouse, oracle_runner
    from tests.unit.sql_compiler.support import view

    env = await TestEnv.create(stack)
    oracle = customer_database()
    try:

        class Authority:
            async def resolve(
                self, principal: Principal, run_id: str, *, trace_id: str | None = None
            ) -> QueryAuthority:
                context = await env.access.resolver.context_for_run(
                    principal, run_id, trace_id=trace_id
                )
                return QueryAuthority(
                    context, view(version=context.product_scope.entitlement_version)
                )

        budgets = build_run_budgets(BackendSettings(), env.db.budgets)
        warehouse = FakeWarehouse(
            oracle_runner(oracle), submit_faults=["lost_response"]
        )
        queries = QueryExecutionService(
            settings=QueryExecutionSettings(project="test-project", location="US"),
            authority=Authority(),
            compilers=COMPILERS,
            boundary=ResultPrivacyBoundary(),
            warehouse=warehouse,
            operations=env.db.tool_executions,
            jobs=env.db.query_jobs,
            admission=budgets,
            usage=budgets,
        )
        env.services = build_investigations(
            BackendSettings(),
            env.db,
            env.access,
            env.scheduler,
            FunctionModel(scripted_model),
            queries=queries,
        )
        from retail_analytics.application.tool_runner import ToolRunnerSettings

        env.services.tools._settings = ToolRunnerSettings(
            follow_seconds=0, max_iterations=3
        )
        run_id = await env.start()
        await env.services.runtime.begin(run_id)
        args: dict[str, JsonValue] = {
            "sql": "SELECT SUM(sale_amount) AS sales FROM sales_items",
            "purpose": "Total completed sales",
        }
        result = await env.services.tools.run(
            run_id, "query-one", "execute_analysis", args
        )
        assert isinstance(result.outcome, ToolSucceeded)
        repeated = await env.services.tools.run(
            run_id, "query-one", "execute_analysis", args
        )
        assert isinstance(repeated.outcome, ToolSucceeded)
        assert repeated.outcome.output.evidence_id == result.outcome.output.evidence_id
        assert len(warehouse.created) == 1
        evidence = await env.db.evidence.candidates(
            env.principal.executive_id, env.session_id
        )
        assert len(evidence) == 1
        budget = await env.db.budgets.get(run_id)
        assert budget and budget.usage.queries == 1
        assert "rows" not in result.outcome.output.model_dump()
    finally:
        oracle.close()
        env.db.close()


async def test_clarification_expiry_does_not_discard_already_recorded_answer(
    stack: Stack,
) -> None:
    env = await TestEnv.create(stack)
    try:
        run_id = await env.start()
        await env.services.runtime.begin(run_id)
        asked = await env.services.runtime.ask(
            QuestionDraft(run_id, 1, "Which sales period should I use?")
        )
        assert asked.question_id is not None
        await env.services.control.answer(
            env.principal,
            run_id=run_id,
            question_id=asked.question_id,
            text="Use annual sales.",
            submission_key="boundary-answer",
        )
        assert (
            await env.services.runtime.expire(run_id)
        ).result is StepResult.SUPERSEDED
        assert (await env.services.runtime.resume(run_id)).result is StepResult.CONTINUE
        assert len(await env.services.inputs.pending(run_id)) == 1
    finally:
        env.db.close()
