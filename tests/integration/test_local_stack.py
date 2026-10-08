"""Local PostgreSQL + Temporal stack checks. Need Docker: ``pytest -m docker``.

Each run starts its own Compose project on free loopback ports and removes only
that project's containers, network and volumes afterwards.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import psycopg
import pytest
from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest
from temporalio.client import Client
from temporalio.worker import Worker

from tests.integration.sentinel_workflow import SentinelWorkflow

pytestmark = pytest.mark.docker

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "compose.yaml"
APP_PASSWORD = "test-app-" + uuid.uuid4().hex
TEMPORAL_PASSWORD = "test-temporal-" + uuid.uuid4().hex
ADMIN_PASSWORD = "test-admin-" + uuid.uuid4().hex


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass(frozen=True)
class Stack:
    project: str
    postgres_port: int
    temporal_port: int

    def compose(self, *args: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["docker", "compose", "-f", str(COMPOSE_FILE), "-p", self.project, *args],  # noqa: S607
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
        if result.returncode != 0:
            raise RuntimeError(f"docker compose {args} failed:\n{result.stderr}")
        return result

    @property
    def env(self) -> dict[str, str]:
        return {
            **os.environ,
            "COMPOSE_POSTGRES_PORT": str(self.postgres_port),
            "COMPOSE_TEMPORAL_PORT": str(self.temporal_port),
            "COMPOSE_APP_DB_PASSWORD": APP_PASSWORD,
            "COMPOSE_TEMPORAL_DB_PASSWORD": TEMPORAL_PASSWORD,
            "COMPOSE_PG_ADMIN_PASSWORD": ADMIN_PASSWORD,
        }

    def dsn(self, user: str, password: str, database: str) -> str:
        return (
            f"host=127.0.0.1 port={self.postgres_port} user={user} "
            f"password={password} dbname={database} connect_timeout=5"
        )

    @property
    def app_url(self) -> str:
        return (
            f"postgresql+psycopg://retail_app:{APP_PASSWORD}"
            f"@127.0.0.1:{self.postgres_port}/retail_app"
        )

    def migrate(self) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "RETAIL_ANALYTICS_DATABASE_URL": self.app_url}
        return subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        )


@pytest.fixture(scope="module")
def stack() -> Iterator[Stack]:
    if shutil.which("docker") is None:
        pytest.skip("docker is not installed")
    probe = subprocess.run(
        ["docker", "info"],  # noqa: S607
        capture_output=True,
        check=False,
        timeout=30,
    )
    if probe.returncode != 0:
        pytest.skip("docker daemon is not reachable")
    stack = Stack(
        project="ra-test-" + uuid.uuid4().hex[:8],
        postgres_port=_free_port(),
        temporal_port=_free_port(),
    )
    try:
        stack.compose("up", "-d", "--wait", "postgres", "temporal")
        stack.compose("run", "--rm", "temporal-namespace")
        yield stack
    finally:
        subprocess.run(
            [  # noqa: S607
                "docker",
                "compose",
                "-f",
                str(COMPOSE_FILE),
                "-p",
                stack.project,
                "down",
                "--volumes",
                "--remove-orphans",
            ],
            env=stack.env,
            capture_output=True,
            check=False,
            timeout=300,
        )


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
    app = stack.dsn("retail_app", APP_PASSWORD, "retail_app")
    assert _scalar(app, "select current_user") == "retail_app"
    for database in ("temporal", "temporal_visibility"):
        dsn = stack.dsn("retail_app", APP_PASSWORD, database)
        with pytest.raises(psycopg.OperationalError, match="permission denied"):
            psycopg.connect(dsn)
    # The Temporal role is equally walled off from application data.
    with pytest.raises(psycopg.OperationalError, match="permission denied"):
        psycopg.connect(stack.dsn("temporal", TEMPORAL_PASSWORD, "retail_app"))
    # Neither service role is a superuser or can create roles/databases.
    roles = _scalar(
        stack.dsn("retail_app", APP_PASSWORD, "retail_app"),
        "select count(*) from pg_roles where rolname in ('retail_app','temporal') "
        "and (rolsuper or rolcreaterole or rolcreatedb)",
    )
    assert roles == 0


def test_migrations_run_twice_without_duplicate_state(stack: Stack) -> None:
    stack.migrate()
    stack.migrate()
    app = stack.dsn("retail_app", APP_PASSWORD, "retail_app")
    assert _scalar(app, "select count(*) from alembic_version") == 1
    assert _scalar(app, "select version_num from alembic_version") == "0001"
    assert _scalar(app, "select count(*) from app_meta") == 1


@pytest.mark.asyncio
async def test_restart_preserves_sentinel_record_and_workflow_history(
    stack: Stack,
) -> None:
    stack.migrate()
    app = stack.dsn("retail_app", APP_PASSWORD, "retail_app")
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
