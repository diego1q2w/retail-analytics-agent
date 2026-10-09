"""The HTTP/SSE API over real PostgreSQL, Temporal and a worker (Docker).

A real uvicorn server runs the production composition (``bootstrap.api``)
with a scripted model in the worker. ``test_local_http_api`` runs the same
tests with local execution (PostgreSQL only, the scripted model in the API
process) by overriding the ``backend`` fixture. Covers: duplicate submissions creating
one run, SSE replay after a client disconnect (later events only, gap-free,
no repeats), two executives (every route answers the other's records as
missing, including SSE and export), authentication failures and the
deletion confirm/replay flow.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
import uvicorn
from pydantic import SecretStr
from pydantic_ai.models.function import FunctionModel

from retail_analytics.adapters.auth.local_jwt import LocalJwtAuthority
from retail_analytics.application.contracts.authorization import Principal
from retail_analytics.bootstrap.api import SchedulerProvider, build_app
from retail_analytics.bootstrap.config import BackendSettings, RuntimeMode
from retail_analytics.bootstrap.execution import local_scheduler
from retail_analytics.domain.runs import ExecutionBackend
from retail_analytics.interfaces.http.services import StreamSettings
from tests.integration.compose_stack import Stack, running_stack
from tests.integration.scripted_investigations import effect_registry, scripted_model
from tests.integration.test_report_deletion import World

pytestmark = [pytest.mark.docker, pytest.mark.asyncio]
ROOT = Path(__file__).resolve().parents[2]
KEY = "docker-test-signing-key-" + "z" * 32
ISSUER, AUDIENCE = "iss", "retail-analytics-api"
AUTHORITY = LocalJwtAuthority(KEY, issuer=ISSUER, audience=AUDIENCE)
SCOPES = ["analysis:read", "reports:read_own", "reports:delete_own"]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def bearer(principal: Principal) -> dict[str, str]:
    raw = AUTHORITY.issue(
        f"sub-{principal.executive_id}", SCOPES, timedelta(minutes=10)
    )
    return {"Authorization": f"Bearer {raw}"}


@dataclass
class Api:
    stack: Stack
    base_url: str
    root: Path
    backend: ExecutionBackend = ExecutionBackend.TEMPORAL


@pytest.fixture(scope="module")
def backend() -> ExecutionBackend:
    """Explicit Temporal execution here; the ``test_local_*`` modules
    override it with local execution."""
    return ExecutionBackend.TEMPORAL


def _scripted_local(settings: BackendSettings) -> SchedulerProvider:
    """Local execution with the same scripted model and tools as the worker."""
    return lambda persistence: local_scheduler(
        settings,
        persistence,
        model=FunctionModel(scripted_model, model_name="scripted"),
        registry=effect_registry(persistence),
    )


@pytest.fixture(scope="module")
def api(
    tmp_path_factory: pytest.TempPathFactory, backend: ExecutionBackend
) -> Iterator[Api]:
    root = tmp_path_factory.mktemp("artifacts")
    temporal = backend is ExecutionBackend.TEMPORAL
    with running_stack("temporal" if temporal else "postgres") as stack:
        worker: subprocess.Popen[str] | None = None
        queue = "t21-" + uuid.uuid4().hex
        if temporal:
            stack.compose("run", "--rm", "temporal-namespace")
        stack.migrate()
        if temporal:
            worker = subprocess.Popen(
                [sys.executable, "-m", "tests.integration.investigation_worker"],
                cwd=ROOT,
                env={
                    **os.environ,
                    "PYTHONPATH": str(ROOT / "src"),
                    "T13_DATABASE_URL": stack.app_url,
                    "T13_TEMPORAL_ADDRESS": f"127.0.0.1:{stack.temporal_port}",
                    "T13_TASK_QUEUE": queue,
                    "T13_CRASH_HOLD": "0",
                    "T14_PROVIDERS": "scripted",
                },
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )
        settings = BackendSettings(
            mode=RuntimeMode.FIXTURE,
            database_url=SecretStr(stack.app_url),
            execution_backend=backend,
            temporal_address=f"127.0.0.1:{stack.temporal_port}" if temporal else None,
            temporal_task_queue=queue,
            auth_signing_key=SecretStr(KEY),
            auth_issuer=ISSUER,
            auth_audience=AUDIENCE,
            artifact_dir=root,
        )
        port = _free_port()
        server = uvicorn.Server(
            uvicorn.Config(
                build_app(
                    settings,
                    StreamSettings(
                        poll_seconds=0.1,
                        heartbeat_seconds=1,
                        max_seconds=60,
                        terminal_grace_seconds=1,
                    ),
                    # Temporal: the production scheduler (worker above).
                    scheduler=None if temporal else _scripted_local(settings),
                ),
                host="127.0.0.1",
                port=port,
                log_level="warning",
            )
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            deadline = datetime.now(UTC) + timedelta(seconds=30)
            while not server.started:
                assert datetime.now(UTC) < deadline, "API did not start"
                assert thread.is_alive(), "API failed to start"
                threading.Event().wait(0.1)
            health = httpx.get(f"http://127.0.0.1:{port}/healthz").json()
            assert health["execution_backend"] == backend.value
            yield Api(stack, f"http://127.0.0.1:{port}", root, backend)
        finally:
            server.should_exit = True
            thread.join(timeout=15)
            if worker is not None:
                worker.kill()
                worker.communicate(timeout=10)


@pytest_asyncio.fixture
async def world(api: Api) -> AsyncIterator[World]:
    w = World(api.stack, api.root)
    yield w
    w.db.close()


@pytest_asyncio.fixture
async def http(api: Api) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=api.base_url, timeout=30) as client:
        yield client


async def retry_until_scheduled(
    http: httpx.AsyncClient, url: str, body: dict[str, object], headers: dict[str, str]
) -> httpx.Response:
    """Temporal may still be warming its namespace cache: the API answers 503
    and the same submission key is retried, as a client would."""
    async with asyncio.timeout(60):
        while True:
            response = await http.post(url, json=body, headers=headers)
            if response.status_code != 503:
                return response
            await asyncio.sleep(0.5)


async def read_events(
    http: httpx.AsyncClient,
    run_id: str,
    headers: dict[str, str],
    *,
    last_event_id: str | None = None,
    stop_after_kind: str | None = None,
) -> list[dict[str, str]]:
    """Read SSE messages; return early (disconnecting) after ``stop_after_kind``."""
    extra = {} if last_event_id is None else {"Last-Event-ID": last_event_id}
    messages: list[dict[str, str]] = []
    async with asyncio.timeout(90):
        async with http.stream(
            "GET", f"/v1/runs/{run_id}/events", headers={**headers, **extra}
        ) as response:
            assert response.status_code == 200, await response.aread()
            fields: dict[str, str] = {}
            async for line in response.aiter_lines():
                if line == "":
                    if fields:
                        messages.append(fields)
                        if fields.get("event") in {stop_after_kind, "end", "error"} - {
                            None
                        }:
                            return messages
                    fields = {}
                    continue
                if line.startswith(":"):
                    continue
                name, _, value = line.partition(": ")
                fields[name] = value
    return messages


async def test_run_lifecycle_replay_after_disconnect_and_two_users(
    http: httpx.AsyncClient, world: World
) -> None:
    alice, _ = await world.executive({"1", "2", "3"})
    bob, _ = await world.executive({"2"})
    a, b = bearer(alice), bearer(bob)

    opened = await http.post("/v1/sessions", json={"submission_key": "s1"}, headers=a)
    assert opened.status_code == 201
    session_id = opened.json()["session_id"]
    again = await http.post("/v1/sessions", json={"submission_key": "s1"}, headers=a)
    assert again.json()["session_id"] == session_id

    body: dict[str, object] = {
        "text": "Analyze sales: clarification case.",
        "submission_key": "q1",
    }
    url = f"/v1/sessions/{session_id}/runs"
    started = await retry_until_scheduled(http, url, body, a)
    assert started.status_code == 202, started.text
    run_id = started.json()["run_id"]
    # A duplicate submission returns the same run instead of creating one.
    duplicate = await http.post(url, json=body, headers=a)
    assert duplicate.status_code == 202
    assert duplicate.json() == {**started.json(), "created": False}

    # First connection: read until the question, then disconnect.
    first = await read_events(http, run_id, a, stop_after_kind="input.required")
    first_events = [m for m in first if "id" in m]
    assert first_events[-1]["event"] == "input.required"
    question = json.loads(first_events[-1]["data"])["input_request"]

    # Bob sees nothing of Alice's: run, events, input, cancel, session.
    for method, path, payload in [
        ("GET", f"/v1/runs/{run_id}", None),
        ("GET", f"/v1/runs/{run_id}/events", None),
        (
            "POST",
            f"/v1/runs/{run_id}/answers",
            {
                "question_id": question["question_id"],
                "text": "x",
                "submission_key": "b",
            },
        ),
        ("POST", f"/v1/runs/{run_id}/steer", {"text": "x", "submission_key": "b2"}),
        ("POST", f"/v1/runs/{run_id}/cancel", None),
        ("GET", f"/v1/sessions/{session_id}", None),
        (
            "POST",
            f"/v1/sessions/{session_id}/runs",
            {"text": "x", "submission_key": "b3"},
        ),
    ]:
        response = await http.request(method, path, json=payload, headers=b)
        assert response.status_code == 404, (path, response.text)
        assert response.json()["error"]["code"] == "not_found"
    assert session_id not in {
        s["session_id"]
        for s in (await http.get("/v1/sessions", headers=b)).json()["sessions"]
    }

    view = (await http.get(f"/v1/runs/{run_id}", headers=a)).json()
    assert view["status"] == "waiting_for_input"
    assert view["question"]["question_id"] == question["question_id"]
    answered = await http.post(
        f"/v1/runs/{run_id}/answers",
        json={
            "question_id": question["question_id"],
            "text": "Use last month.",
            "submission_key": "a1",
        },
        headers=a,
    )
    assert answered.status_code == 202, answered.text

    # Reconnect with the last received ID: only later events, in order.
    second = await read_events(http, run_id, a, last_event_id=first_events[-1]["id"])
    second_events = [m for m in second if "id" in m]
    assert second[-1]["event"] == "end"
    ids = [m["id"] for m in first_events + second_events]
    assert len(ids) == len(set(ids))
    sequences = [
        json.loads(m["data"])["sequence"] for m in first_events + second_events
    ]
    assert sequences == list(range(1, len(sequences) + 1))
    assert second_events[-1]["event"] in ("run.completed", "run.partial")

    done = (await http.get(f"/v1/runs/{run_id}", headers=a)).json()
    assert done["status"] in ("completed", "partial")
    assert done["answer"]["withheld"] is False
    assert done["answer"]["text"]

    # Late steering is refused with a structured answer.
    late = await http.post(
        f"/v1/runs/{run_id}/steer",
        json={"text": "more", "submission_key": "late"},
        headers=a,
    )
    assert late.status_code == 409
    assert late.json()["error"]["code"] == "run_not_active"

    with world.db.engine.connect() as connection:
        count = connection.execute(
            sa.text("SELECT count(*) FROM runs WHERE session_id = :s"),
            {"s": session_id},
        ).scalar_one()
    assert count == 1


async def test_reports_export_and_deletion_are_owner_only(
    http: httpx.AsyncClient, world: World
) -> None:
    alice, session = await world.executive({"1", "2", "3"})
    bob, _ = await world.executive({"1", "2", "3"})
    a, b = bearer(alice), bearer(bob)
    report_id = await world.report(alice, session, title="Quarterly revenue")

    listed = (await http.get("/v1/reports", headers=a)).json()["reports"]
    assert [r["report_id"] for r in listed] == [report_id]
    assert (await http.get("/v1/reports", headers=b)).json()["reports"] == []
    found = await http.get("/v1/reports/search", params={"q": "quarterly"}, headers=a)
    assert [m["report"]["report_id"] for m in found.json()["matches"]] == [report_id]
    exported = await http.get(f"/v1/reports/{report_id}/export", headers=a)
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("text/markdown")
    for path in (
        f"/v1/reports/{report_id}",
        f"/v1/reports/{report_id}/versions",
        f"/v1/reports/{report_id}/export",
    ):
        response = await http.get(path, headers=b)
        assert response.status_code == 404, path

    proposal = await world.propose(alice, session, report_id)
    pid = proposal.proposal_id
    assert (
        await http.get(f"/v1/deletion-proposals/{pid}", headers=b)
    ).status_code == 404
    refused = await http.post(
        f"/v1/deletion-proposals/{pid}/confirm", json={"confirm": True}, headers=b
    )
    assert refused.status_code == 404
    unapproved = await http.post(f"/v1/deletion-proposals/{pid}/confirm", headers=a)
    assert unapproved.status_code == 422
    preview = (await http.get(f"/v1/deletion-proposals/{pid}", headers=a)).json()
    assert preview["count"] == 1 and preview["status"] == "pending"

    confirmed = await http.post(
        f"/v1/deletion-proposals/{pid}/confirm", json={"confirm": True}, headers=a
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["report_ids"] == [report_id]
    replay = await http.post(
        f"/v1/deletion-proposals/{pid}/confirm", json={"confirm": True}, headers=a
    )
    assert replay.status_code == 409
    assert replay.json()["error"]["code"] == "already_resolved"
    gone = await http.get(f"/v1/reports/{report_id}/export", headers=a)
    assert gone.status_code == 404


async def test_authentication_failures(http: httpx.AsyncClient, world: World) -> None:
    alice, _ = await world.executive({"1"})
    good = bearer(alice)
    assert (await http.get("/v1/sessions", headers=good)).status_code == 200

    expired = LocalJwtAuthority(
        KEY,
        issuer=ISSUER,
        audience=AUDIENCE,
        clock=lambda: datetime.now(UTC) - timedelta(days=2),
    ).issue(f"sub-{alice.executive_id}", SCOPES, timedelta(minutes=5))
    forged = LocalJwtAuthority(
        "another-key-" + "q" * 32, issuer=ISSUER, audience=AUDIENCE
    ).issue(f"sub-{alice.executive_id}", SCOPES, timedelta(minutes=5))
    wrong_audience = LocalJwtAuthority(KEY, issuer=ISSUER, audience="other").issue(
        f"sub-{alice.executive_id}", SCOPES, timedelta(minutes=5)
    )
    unknown = AUTHORITY.issue("sub-nobody", SCOPES, timedelta(minutes=5))
    for headers in (
        {},
        {"Authorization": f"Bearer {expired}"},
        {"Authorization": f"Bearer {forged}"},
        {"Authorization": f"Bearer {wrong_audience}"},
        {"Authorization": f"Bearer {unknown}"},
    ):
        for path in ("/v1/sessions", "/v1/runs/any/events", "/v1/reports"):
            response = await http.get(path, headers=headers)
            assert response.status_code == 401, (path, headers.keys())
            assert response.json()["error"]["code"] == "unauthenticated"

    await world.db.access_admin.set_active(alice.executive_id, False)
    deactivated = await http.get("/v1/sessions", headers=good)
    assert deactivated.status_code == 401
