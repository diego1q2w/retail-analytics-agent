"""PostgreSQL persistence: idempotency, run ownership, rollback and restart.

Needs Docker (``pytest -m docker``); runs against a throwaway Compose project.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from dataclasses import replace

import psycopg
import pytest
import sqlalchemy as sa

from retail_analytics.application.contracts import Correlation
from retail_analytics.application.contracts.persistence import (
    ActiveRunExists,
    IdempotencyConflict,
    OperationRequest,
    RecordNotFound,
    RunRequest,
    RunStart,
)
from retail_analytics.application.contracts.progress import (
    EventKind,
    ProgressUpdate,
    ToolActivity,
)
from retail_analytics.bootstrap.persistence import Persistence, build_persistence
from retail_analytics.domain.executions import QueryJob, ToolExecutionStatus
from retail_analytics.domain.operations import SideEffect, ToolErrorCode
from retail_analytics.domain.runs import RunStatus, WorkflowRef
from tests.integration.compose_stack import Stack, running_stack

pytestmark = pytest.mark.docker
S = ToolExecutionStatus


@pytest.fixture(scope="module")
def stack() -> Iterator[Stack]:
    with running_stack("postgres") as stack:
        stack.migrate()
        yield stack


@pytest.fixture
def db(stack: Stack) -> Iterator[Persistence]:
    persistence = build_persistence(stack.app_url)
    yield persistence
    persistence.close()


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _count(stack: Stack, sql: str, *params: object) -> int:
    with psycopg.connect(stack.app_dsn) as conn:
        row = conn.execute(sql, params).fetchone()
    assert row is not None
    return int(row[0])


def _request(session_id: str, executive: str, key: str | None = None) -> RunRequest:
    key = key or _id("key")
    return RunRequest(
        run_id=_id("run"),
        session_id=session_id,
        requested_by=executive,
        submission_key=key,
        message_id=_id("msg"),
        request_text=f"How did revenue change? ({key})",
    )


async def _session_with_run(db: Persistence) -> tuple[str, RunStart]:
    executive = _id("exec")
    session = await db.sessions.create_session(_id("ses"), executive)
    started = await db.runs.start_run(_request(session.session_id, executive))
    return executive, started


def _operation(run_id: str) -> OperationRequest:
    return OperationRequest(
        operation_id=_id("op"),
        run_id=run_id,
        capability="run_query",
        capability_version=1,
        side_effect=SideEffect.EXTERNAL_JOB,
    )


def _update(start: RunStart, kind: EventKind, summary: str) -> ProgressUpdate:
    return ProgressUpdate(
        correlation=Correlation(
            session_id=start.run.session_id, run_id=start.run.run_id
        ),
        kind=kind,
        summary=summary,
    )


@pytest.mark.asyncio
async def test_same_operation_key_yields_exactly_one_execution(
    stack: Stack, db: Persistence
) -> None:
    _, started = await _session_with_run(db)
    request = _operation(started.run.run_id)
    other_process = build_persistence(stack.app_url)
    try:
        results = await asyncio.gather(
            *(db.tool_executions.begin(request) for _ in range(10)),
            *(other_process.tool_executions.begin(request) for _ in range(10)),
        )
    finally:
        other_process.close()

    assert sum(result.created for result in results) == 1
    assert {result.execution.operation_id for result in results} == {
        request.operation_id
    }
    op = request.operation_id
    assert (
        _count(
            stack, "select count(*) from tool_executions where operation_id = %s", op
        )
        == 1
    )
    assert (
        _count(
            stack, "select count(*) from execution_events where operation_id = %s", op
        )
        == 1
    )

    with pytest.raises(IdempotencyConflict):
        await db.tool_executions.begin(
            OperationRequest(
                operation_id=request.operation_id,
                run_id=started.run.run_id,
                capability="other_tool",
                capability_version=1,
                side_effect=SideEffect.READ_ONLY,
            )
        )


@pytest.mark.asyncio
async def test_repeated_transition_reports_are_recorded_once(db: Persistence) -> None:
    _, started = await _session_with_run(db)
    op = (await db.tool_executions.begin(_operation(started.run.run_id))).execution
    reports = [
        (S.SUBMITTING, 1, None),
        (S.RUNNING, 1, None),
        (S.RUNNING, 1, None),  # a retried activity reporting the same fact
        (S.RETRYING, 1, ToolErrorCode.TEMPORARY_FAILURE),
        (S.RUNNING, 2, None),
        (S.SUCCEEDED, 2, None),
        (S.SUCCEEDED, 2, None),
    ]
    for status, attempt, code in reports:
        await db.tool_executions.transition(
            op.operation_id, status, attempt=attempt, error_code=code
        )

    current = await db.tool_executions.get(op.operation_id)
    assert current is not None
    assert (current.status, current.attempt_count) == (S.SUCCEEDED, 2)
    history = await db.tool_executions.history(op.operation_id)
    assert [(e.sequence, e.to_status, e.attempt) for e in history] == [
        (1, S.PREPARED, 0),
        (2, S.SUBMITTING, 1),
        (3, S.RUNNING, 1),
        (4, S.RETRYING, 1),
        (5, S.RUNNING, 2),
        (6, S.SUCCEEDED, 2),
    ]
    assert history[3].error_code is ToolErrorCode.TEMPORARY_FAILURE
    assert [e.from_status for e in history[1:]] == [e.to_status for e in history[:-1]]


@pytest.mark.asyncio
async def test_only_one_active_run_per_session_under_concurrent_starts(
    stack: Stack, db: Persistence
) -> None:
    executive = _id("exec")
    session = await db.sessions.create_session(_id("ses"), executive)
    requests = [_request(session.session_id, executive) for _ in range(12)]

    results = await asyncio.gather(
        *(db.runs.start_run(r) for r in requests), return_exceptions=True
    )

    started = [r for r in results if isinstance(r, RunStart)]
    rejected = [r for r in results if isinstance(r, ActiveRunExists)]
    assert len(started) == 1
    assert len(rejected) == len(requests) - 1
    winner = started[0].run
    assert {r.active_run_id for r in rejected} == {winner.run_id}
    sid = session.session_id
    assert _count(stack, "select count(*) from runs where session_id = %s", sid) == 1
    # Rejected requests left no orphan triggering messages behind.
    assert (
        _count(stack, "select count(*) from messages where session_id = %s", sid) == 1
    )
    active = await db.runs.active_run(sid)
    assert active is not None
    assert active.run_id == winner.run_id

    # The database itself refuses a second active run, even bypassing the lock.
    with (
        psycopg.connect(stack.app_dsn) as conn,
        pytest.raises(psycopg.errors.UniqueViolation) as violation,
    ):
        conn.execute(
            "insert into messages (message_id, session_id, role, content, created_at)"
            " values ('bypass', %s, 'user', 'x', now())",
            (sid,),
        )
        conn.execute(
            "insert into runs (run_id, session_id, requested_by, trigger_message_id,"
            " submission_key, status, created_at, updated_at)"
            " values ('bypass', %s, %s, 'bypass', 'bypass', 'running', now(), now())",
            (sid, executive),
        )
    assert violation.value.diag.constraint_name == "ux_runs_one_active_per_session"

    # Once the run ends, the session accepts the next investigation.
    await db.runs.transition_run(winner.run_id, RunStatus.COMPLETED)
    nxt = await db.runs.start_run(_request(sid, executive))
    assert nxt.created


@pytest.mark.asyncio
async def test_separate_sessions_run_concurrently(db: Persistence) -> None:
    executive = _id("exec")
    sessions = [
        await db.sessions.create_session(_id("ses"), executive) for _ in range(6)
    ]
    results = await asyncio.gather(
        *(db.runs.start_run(_request(s.session_id, executive)) for s in sessions)
    )
    assert all(result.created for result in results)
    assert {r.run.session_id for r in results} == {s.session_id for s in sessions}


@pytest.mark.asyncio
async def test_duplicate_submission_returns_the_original_run(
    stack: Stack, db: Persistence
) -> None:
    executive = _id("exec")
    session = await db.sessions.create_session(_id("ses"), executive)
    first = _request(session.session_id, executive, key="submit-1")
    # Retries of one submission carry fresh server-side IDs but the same key.
    retries = [
        RunRequest(
            run_id=_id("run"),
            session_id=first.session_id,
            requested_by=executive,
            submission_key=first.submission_key,
            message_id=_id("msg"),
            request_text=first.request_text,
        )
        for _ in range(5)
    ]
    results = await asyncio.gather(*(db.runs.start_run(r) for r in [first, *retries]))

    assert sum(r.created for r in results) == 1
    assert len({r.run.run_id for r in results}) == 1
    assert len({r.message.message_id for r in results}) == 1
    sid = session.session_id
    assert _count(stack, "select count(*) from runs where session_id = %s", sid) == 1

    with pytest.raises(IdempotencyConflict):
        await db.runs.start_run(
            RunRequest(
                run_id=_id("run"),
                session_id=sid,
                requested_by=executive,
                submission_key=first.submission_key,
                message_id=_id("msg"),
                request_text="A different question",
            )
        )


@pytest.mark.asyncio
async def test_runs_stay_linked_to_their_session_owner(db: Persistence) -> None:
    owner = _id("exec")
    session = await db.sessions.create_session(_id("ses"), owner)
    with pytest.raises(RecordNotFound):
        await db.runs.start_run(_request(session.session_id, _id("intruder")))
    with pytest.raises(IdempotencyConflict):
        await db.sessions.create_session(session.session_id, _id("intruder"))

    _, started = await _session_with_run(db)
    foreign = ProgressUpdate(
        correlation=Correlation(
            session_id=session.session_id, run_id=started.run.run_id
        ),
        kind=EventKind.RUN_STARTED,
        summary="Started",
    )
    with pytest.raises(RecordNotFound):
        await db.run_events.append(foreign)


@pytest.mark.asyncio
async def test_failed_writes_roll_back_completely(
    stack: Stack, db: Persistence
) -> None:
    _, started = await _session_with_run(db)
    request = _operation(started.run.run_id)
    await db.run_events.append(_update(started, EventKind.RUN_STARTED, "Started"))

    def fail_history_inserts(
        conn: object, cursor: object, statement: str, *args: object
    ) -> None:
        if (
            statement.lstrip()
            .upper()
            .startswith(("INSERT INTO EXECUTION_EVENTS", "INSERT INTO RUN_EVENTS"))
        ):
            raise RuntimeError("injected failure")

    sa.event.listen(db.engine, "before_cursor_execute", fail_history_inserts)
    try:
        with pytest.raises(RuntimeError, match="injected"):
            await db.tool_executions.begin(request)
        with pytest.raises(RuntimeError, match="injected"):
            await db.run_events.append(
                _update(started, EventKind.ANALYSIS_PROGRESS, "Lost")
            )
    finally:
        sa.event.remove(db.engine, "before_cursor_execute", fail_history_inserts)

    op = request.operation_id
    assert (
        _count(
            stack, "select count(*) from tool_executions where operation_id = %s", op
        )
        == 0
    )
    run_id = started.run.run_id
    assert (
        _count(stack, "select last_event_sequence from runs where run_id = %s", run_id)
        == 1
    )
    # The same keys work once the fault is gone, with no gaps in the sequence.
    assert (await db.tool_executions.begin(request)).created
    event = await db.run_events.append(
        _update(started, EventKind.ANALYSIS_PROGRESS, "Ok")
    )
    assert event.sequence == 2


@pytest.mark.asyncio
async def test_histories_are_append_only(stack: Stack, db: Persistence) -> None:
    _, started = await _session_with_run(db)
    await db.tool_executions.begin(_operation(started.run.run_id))
    await db.run_events.append(_update(started, EventKind.RUN_STARTED, "Started"))
    for table in ("execution_events", "run_events"):
        with (
            psycopg.connect(stack.app_dsn) as conn,
            pytest.raises(psycopg.errors.RestrictViolation, match="append-only"),
        ):
            conn.execute(f"update {table} set occurred_at = now()")  # noqa: S608


@pytest.mark.asyncio
async def test_concurrent_events_get_gap_free_order_and_replay(
    db: Persistence,
) -> None:
    _, started = await _session_with_run(db)
    appended = await asyncio.gather(
        *(
            db.run_events.append(_update(started, EventKind.ANALYSIS_PROGRESS, f"s{i}"))
            for i in range(15)
        )
    )
    assert sorted(e.sequence for e in appended) == list(range(1, 16))

    replayed = await db.run_events.replay(started.run.run_id)
    assert [e.sequence for e in replayed] == list(range(1, 16))
    after = await db.run_events.replay(
        started.run.run_id, after_event_id=replayed[9].event_id
    )
    assert [e.event_id for e in after] == [e.event_id for e in replayed[10:]]
    with pytest.raises(RecordNotFound):
        await db.run_events.replay(started.run.run_id, after_event_id="not-an-event")


@pytest.mark.asyncio
async def test_query_job_reference_is_recorded_once(db: Persistence) -> None:
    _, started = await _session_with_run(db)
    op = (await db.tool_executions.begin(_operation(started.run.run_id))).execution
    job = QueryJob(
        operation_id=op.operation_id,
        job_id=_id("job"),
        project="analytics-project",
        location="US",
        query_fingerprint="fp-1",
        query_ref="protected/ref-1",
        authorization_version=3,
        catalog_version="cat-1",
    )
    assert await db.query_jobs.register_job(job) == job
    assert await db.query_jobs.register_job(job) == job
    assert await db.query_jobs.get_job(op.operation_id) == job

    other = (await db.tool_executions.begin(_operation(started.run.run_id))).execution
    with pytest.raises(IdempotencyConflict):
        await db.query_jobs.register_job(replace(job, operation_id=other.operation_id))


@pytest.mark.asyncio
async def test_query_job_submissions_follow_each_other(db: Persistence) -> None:
    _, started = await _session_with_run(db)
    op = (await db.tool_executions.begin(_operation(started.run.run_id))).execution
    first = QueryJob(
        operation_id=op.operation_id,
        job_id=_id("job"),
        project="analytics-project",
        location="US",
        query_fingerprint="fp-1",
        query_ref="protected/ref-1",
        authorization_version=3,
        catalog_version="cat-1",
    )
    second = replace(first, job_id=_id("job"), query_fingerprint="fp-2", submission=2)

    with pytest.raises(IdempotencyConflict):
        await db.query_jobs.register_job(second)  # must follow submission 1
    assert await db.query_jobs.register_job(first) == first
    assert await db.query_jobs.register_job(second) == second
    assert await db.query_jobs.register_job(second) == second
    assert await db.query_jobs.get_job(op.operation_id) == second
    assert list(await db.query_jobs.jobs(op.operation_id)) == [first, second]
    with pytest.raises(IdempotencyConflict):
        await db.query_jobs.register_job(replace(second, query_fingerprint="fp-x"))
    with pytest.raises(IdempotencyConflict):
        await db.query_jobs.register_job(
            replace(second, submission=3, job_id=first.job_id)
        )


@pytest.mark.asyncio
async def test_state_and_events_survive_database_and_process_restart(
    stack: Stack, db: Persistence
) -> None:
    executive, started = await _session_with_run(db)
    run_id = started.run.run_id
    await db.runs.attach_workflow(run_id, WorkflowRef("wf-" + run_id, "temporal-run-1"))
    op = (await db.tool_executions.begin(_operation(run_id))).execution
    await db.tool_executions.transition(op.operation_id, S.RUNNING, attempt=1)
    await db.runs.transition_run(run_id, RunStatus.WAITING_FOR_INPUT)
    await db.run_events.append(_update(started, EventKind.RUN_STARTED, "Started"))
    before = await db.run_events.append(
        _update(started, EventKind.ANALYSIS_PROGRESS, "Checking revenue")
    )
    db.close()

    stack.compose("restart", "postgres")
    stack.compose("up", "-d", "--wait", "postgres")

    fresh = build_persistence(stack.app_url)
    try:
        run = await fresh.runs.get_run(run_id)
        assert run is not None
        assert run.status is RunStatus.WAITING_FOR_INPUT
        assert (run.requested_by, run.session_id) == (
            executive,
            started.run.session_id,
        )
        assert run.workflow == WorkflowRef("wf-" + run_id, "temporal-run-1")
        assert run.run_id != run.workflow.workflow_id
        trigger = await fresh.sessions.recent_messages(run.session_id, 10)
        assert [(m.message_id, m.run_id) for m in trigger] == [
            (run.trigger_message_id, run_id)
        ]

        operation = await fresh.tool_executions.get(op.operation_id)
        assert operation is not None
        assert (operation.status, operation.run_id) == (S.RUNNING, run_id)
        assert [
            e.to_status for e in await fresh.tool_executions.history(op.operation_id)
        ] == [
            S.PREPARED,
            S.RUNNING,
        ]

        events = await fresh.run_events.replay(run_id)
        assert [(e.sequence, e.summary) for e in events] == [
            (1, "Started"),
            (2, "Checking revenue"),
        ]
        assert events[-1] == before
        resumed = await fresh.runs.transition_run(run_id, RunStatus.RUNNING)
        assert resumed.status is RunStatus.RUNNING
        tool_update = ProgressUpdate(
            correlation=Correlation(
                session_id=run.session_id, run_id=run_id, operation_id=op.operation_id
            ),
            kind=EventKind.TOOL_SUCCEEDED,
            summary="Completed.",
            tool=ToolActivity(capability="run_query", capability_version=1),
        )
        await fresh.run_events.publish(tool_update)
        after = await fresh.run_events.replay(run_id, after_event_id=before.event_id)
        assert [(e.sequence, e.kind) for e in after] == [(3, EventKind.TOOL_SUCCEEDED)]
        assert after[0].correlation.operation_id == op.operation_id
    finally:
        fresh.close()
