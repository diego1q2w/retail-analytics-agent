"""The integrated agent conversations on the local manager, without Temporal.

Runs the acceptance conversations of ``test_agent_runtime`` (schema
inspection, guarded analysis, cited evidence, saved/read reports, held-out
privacy/scope checks, retrieved-instruction attacks that try to confirm a
deletion or change authority, steering, truncation) unchanged, with the
in-process local manager as the execution backend and PostgreSQL as the only
service.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from pydantic import SecretStr

from retail_analytics.application.contracts.authorization import Principal
from retail_analytics.application.investigation_runtime import INTERRUPTED_NOTICE
from retail_analytics.bootstrap.agent_evaluation import _PERMISSIONS, _Loop
from retail_analytics.bootstrap.config import BackendSettings
from retail_analytics.domain.runs import ExecutionBackend, RunStatus
from tests.integration import local_process
from tests.integration.compose_stack import Stack, running_stack
from tests.integration.test_agent_runtime import (
    test_answer_citing_a_cut_result_is_never_recorded_as_complete,
    test_conversation_retrieves_methods_queries_and_saves_cited_report,
    test_fresh_evidence_answers_follow_up_without_query_and_no_example_path,
    test_heldout_manifest_with_scripted_plans,
    test_realdata_manifest_from_frozen_extract,
    test_retrieved_instructions_cannot_approve_deletion_or_change_authority,
    test_user_steering_redirects_the_active_analysis,
)

pytestmark = [pytest.mark.docker]
ROOT = Path(__file__).resolve().parents[2]

__all__ = [
    "test_answer_citing_a_cut_result_is_never_recorded_as_complete",
    "test_conversation_retrieves_methods_queries_and_saves_cited_report",
    "test_fresh_evidence_answers_follow_up_without_query_and_no_example_path",
    "test_heldout_manifest_with_scripted_plans",
    "test_realdata_manifest_from_frozen_extract",
    "test_retrieved_instructions_cannot_approve_deletion_or_change_authority",
    "test_user_steering_redirects_the_active_analysis",
]


@pytest.fixture(scope="module")
def stack() -> Iterator[Stack]:
    with running_stack("postgres") as stack:
        stack.migrate()
        yield stack


@pytest.fixture(scope="module")
def backend() -> ExecutionBackend:
    return ExecutionBackend.LOCAL


@pytest.fixture(scope="module")
def settings(stack: Stack, tmp_path_factory: pytest.TempPathFactory) -> BackendSettings:
    # No Temporal address: the local backend must not need one.
    return BackendSettings(
        database_url=SecretStr(stack.app_url),
        artifact_dir=tmp_path_factory.mktemp("artifacts"),
    )


def _rows(stack: Stack, sql: str, **params: Any) -> list[Any]:
    engine = sa.create_engine(stack.app_url)
    try:
        with engine.connect() as connection:
            return list(connection.execute(sa.text(sql), params))
    finally:
        engine.dispose()


def _wait_for(
    stack: Stack, sql: str, process: subprocess.Popen[str], **params: Any
) -> None:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if _rows(stack, sql, **params)[0][0]:
            return
        if process.poll() is not None:
            _, stderr = process.communicate()
            pytest.fail("local process exited: " + stderr[-4000:])
        time.sleep(0.2)
    pytest.fail("local process did not reach the expected state")


def test_killed_process_interrupts_runs_without_replay_and_accepts_new_request(
    settings: BackendSettings, stack: Stack
) -> None:
    """SIGKILL the process mid-query; the next manager tells the truth.

    No model call or warehouse submission is repeated, the uncertain job is
    reconciled by its recorded job ID, the clarification and the queued
    request are closed with notices (kept as history), and the session takes
    a new explicit request.
    """
    process = subprocess.Popen(
        [sys.executable, "-m", "tests.integration.local_process"],
        cwd=ROOT,
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT / "src"),
            "LOCAL_DATABASE_URL": stack.app_url,
            "LOCAL_ARTIFACT_DIR": str(settings.artifact_dir),
            "LOCAL_SCENARIO": "local-kill",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        line = process.stdout.readline()
        if not line:
            _, stderr = process.communicate()
            pytest.fail("local process failed: " + stderr[-4000:])
        ids = json.loads(line)
        held, waiting = ids["held"], ids["waiting"]
        _wait_for(
            stack,
            "SELECT count(*) FROM query_executions AS q JOIN tool_executions AS t "
            "ON t.operation_id = q.operation_id WHERE t.run_id = :r",
            process,
            r=held,
        )
        _wait_for(
            stack,
            "SELECT count(*) FROM runs WHERE run_id = :r AND status = :s",
            process,
            r=waiting,
            s=RunStatus.WAITING_FOR_INPUT.value,
        )
    finally:
        process.kill()
        process.communicate(timeout=30)

    def usage(run_id: str) -> Any:
        return _rows(
            stack,
            "SELECT provider_requests, tokens, queries, bytes FROM run_budgets "
            "WHERE run_id = :r",
            r=run_id,
        )

    jobs_before = _rows(
        stack,
        "SELECT q.operation_id, q.submission, q.job_id FROM query_executions AS q "
        "JOIN tool_executions AS t ON t.operation_id = q.operation_id "
        "WHERE t.run_id = :r",
        r=held,
    )
    assert len(jobs_before) == 1
    usage_before = usage(held), usage(waiting)
    # The killed process left its runs active: nothing recorded an outcome.
    left = _rows(stack, "SELECT status FROM runs WHERE run_id = :r", r=held)
    assert [r[0] for r in left] == ["running"]

    restarted = local_process.target(settings, hold=False)
    warehouse = restarted.source.warehouse
    assert isinstance(warehouse, local_process.HeldWarehouse)
    principal = Principal(ids["executive"], _PERMISSIONS)

    async def restart() -> str:
        harness = await restarted._start()  # lock, orphan sweep, admit
        assert harness.manager is not None
        assert harness.manager.instance_id != ids["instance"]
        assert harness.manager.running() == frozenset()
        handle = await harness.services.control.start(
            principal,
            session_id=ids["session"],
            text=local_process.FRESH,
            submission_key=uuid.uuid4().hex,
        )
        await restarted._wait(harness.persistence, handle.run_id)
        return handle.run_id

    loop = _Loop()
    restarted._loop = loop
    try:
        fresh = loop.run(restart(), timeout=180)
    finally:
        restarted.close()

    status = {
        r[0]: r[1]
        for r in _rows(
            stack,
            "SELECT run_id, status FROM runs WHERE run_id IN (:a, :b, :c)",
            a=held,
            b=waiting,
            c=fresh,
        )
    }
    assert status == {held: "failed", waiting: "failed", fresh: "completed"}
    # No replay: the same single submission, cancellation requested for the
    # recorded job, no new job and no model request for the old runs.
    jobs_after = _rows(
        stack,
        "SELECT q.operation_id, q.submission, q.job_id FROM query_executions AS q "
        "JOIN tool_executions AS t ON t.operation_id = q.operation_id "
        "WHERE t.run_id = :r",
        r=held,
    )
    assert jobs_after == jobs_before
    assert warehouse.cancelled == [jobs_before[0][2]]
    assert warehouse.submitted == 1  # only the new request's query
    assert (usage(held), usage(waiting)) == usage_before
    operations = _rows(
        stack, "SELECT status FROM tool_executions WHERE run_id = :r", r=held
    )
    assert [o[0] for o in operations] == ["cancelled"]
    # Truthful notices; the queued request was discarded, never started.
    texts = [
        r[0]
        for r in _rows(
            stack,
            "SELECT content FROM messages WHERE session_id = :s AND role = "
            "'assistant' ORDER BY created_at",
            s=ids["session"],
        )
    ]
    assert any(
        INTERRUPTED_NOTICE in t and "queued request was not started" in t for t in texts
    )
    assert not any("The queued request ran." in t for t in texts)
    queued = _rows(
        stack,
        "SELECT status, promoted_run_id FROM run_inputs WHERE input_id = :i",
        i=ids["queued"],
    )
    assert queued == [("discarded", None)]
    questions = _rows(
        stack, "SELECT status FROM run_questions WHERE run_id = :r", r=waiting
    )
    assert [q[0] for q in questions] == ["closed"]
    # History, evidence and budgets are kept; the old run is attributed to
    # the dead local instance, never to Temporal.
    owners = _rows(
        stack,
        "SELECT execution_backend, local_execution_id, temporal_workflow_id "
        "FROM runs WHERE run_id = :r",
        r=held,
    )
    assert owners == [("local", ids["instance"], None)]
    events = [
        r[0]
        for r in _rows(
            stack,
            "SELECT kind FROM run_events WHERE run_id = :r ORDER BY sequence",
            r=held,
        )
    ]
    assert events[0] == "run.started" and events[-1] == "run.failed"
