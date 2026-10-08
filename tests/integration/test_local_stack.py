"""Local PostgreSQL + Temporal stack checks. Need Docker: ``pytest -m docker``.

Each run starts its own Compose project on free loopback ports and removes only
that project's containers, network and volumes afterwards.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import timedelta

import psycopg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest
from temporalio.client import Client
from temporalio.worker import Worker

from tests.integration.compose_stack import ROOT, Stack, running_stack
from tests.integration.sentinel_workflow import SentinelWorkflow

pytestmark = pytest.mark.docker


@pytest.fixture(scope="module")
def stack() -> Iterator[Stack]:
    with running_stack("postgres", "temporal") as stack:
        stack.compose("run", "--rm", "temporal-namespace")
        yield stack


def _head_revision() -> str:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    head = ScriptDirectory.from_config(config).get_current_head()
    assert head is not None
    return head


def _scalar(dsn: str, sql: str) -> object:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(sql).fetchone()
    assert row is not None
    return row[0]


async def _run_sentinel_workflow(stack: Stack, workflow_id: str) -> None:
    client = await Client.connect(f"127.0.0.1:{stack.temporal_port}")
    async with Worker(client, task_queue=workflow_id, workflows=[SentinelWorkflow]):
        result = await client.execute_workflow(
            SentinelWorkflow.run,
            "t02",
            id=workflow_id,
            task_queue=workflow_id,
            execution_timeout=timedelta(seconds=60),
        )
    assert result == "hello t02"


async def _history_event_count(stack: Stack, workflow_id: str) -> int:
    client = await Client.connect(f"127.0.0.1:{stack.temporal_port}")
    history = await client.get_workflow_handle(workflow_id).fetch_history()
    return len(history.events)


def test_services_are_healthy(stack: Stack) -> None:
    out = stack.compose("ps", "--format", "{{.Service}} {{.Health}}", "--all")
    states = {
        parts[0]: parts[1] if len(parts) > 1 else ""
        for parts in (line.split(" ", 1) for line in out.stdout.splitlines() if line)
    }
    assert states["postgres"].strip() == "healthy"
    assert states["temporal"].strip() == "healthy"


@pytest.mark.asyncio
async def test_default_namespace_keeps_closed_histories_seven_days(
    stack: Stack,
) -> None:
    client = await Client.connect(f"127.0.0.1:{stack.temporal_port}")
    described = await client.workflow_service.describe_namespace(
        DescribeNamespaceRequest(namespace="default")
    )
    assert described.config.workflow_execution_retention_ttl.seconds == 7 * 86400


def test_application_role_is_isolated_from_temporal_data(stack: Stack) -> None:
    app = stack.app_dsn
    assert _scalar(app, "select current_user") == "retail_app"
    for database in ("temporal", "temporal_visibility"):
        dsn = stack.dsn("retail_app", stack.app_password, database)
        with pytest.raises(psycopg.OperationalError, match="permission denied"):
            psycopg.connect(dsn)
    # The Temporal role is equally walled off from application data.
    with pytest.raises(psycopg.OperationalError, match="permission denied"):
        psycopg.connect(stack.dsn("temporal", stack.temporal_password, "retail_app"))
    # Neither service role is a superuser or can create roles/databases.
    roles = _scalar(
        stack.app_dsn,
        "select count(*) from pg_roles where rolname in ('retail_app','temporal') "
        "and (rolsuper or rolcreaterole or rolcreatedb)",
    )
    assert roles == 0


def test_migrations_run_twice_without_duplicate_state(stack: Stack) -> None:
    stack.migrate()
    stack.migrate()
    app = stack.app_dsn
    assert _scalar(app, "select count(*) from alembic_version") == 1
    assert _scalar(app, "select version_num from alembic_version") == (_head_revision())
    assert _scalar(app, "select count(*) from app_meta") == 1


@pytest.mark.asyncio
async def test_restart_preserves_sentinel_record_and_workflow_history(
    stack: Stack,
) -> None:
    stack.migrate()
    app = stack.app_dsn
    with psycopg.connect(app) as conn:
        conn.execute(
            "insert into app_meta (key, value) values ('restart_sentinel', 'kept') "
            "on conflict (key) do nothing"
        )
    workflow_id = "t02-" + uuid.uuid4().hex
    await _run_sentinel_workflow(stack, workflow_id)
    before = await _history_event_count(stack, workflow_id)
    assert before >= 3

    stack.compose("restart", "postgres", "temporal")
    stack.compose("up", "-d", "--wait", "postgres", "temporal")

    assert (
        _scalar(app, "select value from app_meta where key = 'restart_sentinel'")
        == "kept"
    )
    assert await _history_event_count(stack, workflow_id) == before
