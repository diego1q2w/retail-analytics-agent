"""The HTTP/SSE API tests of ``test_http_api`` with local execution (default).

PostgreSQL is the only service: no Temporal container, namespace or worker.
The production API composition hosts the local manager in its lifespan, with
the same scripted model and tools the Temporal worker uses there. Duplicate
submissions, SSE replay after a disconnect, two executives, authentication
failures and the human-confirmed deletion flow run unchanged.
"""

# ruff: noqa: F811
from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from retail_analytics.bootstrap import api as api_composition
from retail_analytics.bootstrap.api import build_app
from retail_analytics.bootstrap.config import BackendSettings, RuntimeMode
from retail_analytics.bootstrap.execution import ExecutionStartupError
from retail_analytics.domain.runs import ExecutionBackend
from tests.integration.test_http_api import (  # noqa: F401  (fixtures reused)
    KEY,
    Api,
    api,
    http,
    test_authentication_failures,
    test_reports_export_and_deletion_are_owner_only,
    test_run_lifecycle_replay_after_disconnect_and_two_users,
    world,
)

pytestmark = [pytest.mark.docker, pytest.mark.asyncio]


@pytest.fixture(scope="module")
def backend() -> ExecutionBackend:
    return ExecutionBackend.LOCAL


async def test_a_second_local_api_on_the_same_database_fails_clearly(
    api: Api, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """One process hosts local investigations per database: a second API's
    startup fails with an actionable message instead of sharing runs."""

    # Earlier tests seed runs directly through services (stored with the
    # schema default backend), which the ownership check would report first;
    # this test is about the lock.
    async def no_foreign_work(*_args: object) -> None:
        return None

    monkeypatch.setattr(api_composition, "ensure_owned_work", no_foreign_work)
    second = build_app(
        BackendSettings(
            mode=RuntimeMode.FIXTURE,
            database_url=SecretStr(api.stack.app_url),
            auth_signing_key=SecretStr(KEY),
            artifact_dir=api.root,
        )
    )

    def start() -> None:
        with TestClient(second):
            pass

    with pytest.raises(ExecutionStartupError):
        await asyncio.to_thread(start)
    err = capsys.readouterr().err
    assert "cannot start: another API process with local execution" in err
    # The first API is unaffected.
    async with httpx.AsyncClient(base_url=api.base_url) as client:
        health = (await client.get("/healthz")).json()
    assert health["execution_backend"] == "local"
