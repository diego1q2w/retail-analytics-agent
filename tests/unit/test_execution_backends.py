"""Execution-backend selection: ownership checks, startup errors, defaults.

No database: ports are faked; the local API startup path is probed in a fresh
interpreter that refuses every Temporal import.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from retail_analytics.application.contracts.execution_backends import (
    ForeignExecutions,
)
from retail_analytics.application.execution_backends import (
    ExecutionOwnership,
    IncompatibleActiveExecutions,
    other_backend,
)
from retail_analytics.bootstrap import api, execution
from retail_analytics.bootstrap.agent_evaluation import (
    AgentRuntimeTarget,
    heldout_source,
)
from retail_analytics.bootstrap.config import BackendSettings
from retail_analytics.bootstrap.persistence import Persistence
from retail_analytics.domain.runs import ExecutionBackend

REPO = Path(__file__).resolve().parents[2]
LOCAL, TEMPORAL = ExecutionBackend.LOCAL, ExecutionBackend.TEMPORAL


class FakeActive:
    def __init__(self, by_backend: dict[ExecutionBackend, ForeignExecutions]) -> None:
        self.by_backend = by_backend
        self.asked: list[ExecutionBackend] = []

    async def owned_by(
        self, backend: ExecutionBackend, *, sample: int
    ) -> ForeignExecutions:
        self.asked.append(backend)
        return self.by_backend.get(backend, ForeignExecutions(backend, 0, 0))


@pytest.mark.asyncio
async def test_ownership_checks_only_the_other_backend() -> None:
    busy = ForeignExecutions(TEMPORAL, 2, 1, ("run-a", "run-b"))
    active = FakeActive({TEMPORAL: busy})
    ownership = ExecutionOwnership(active)
    with pytest.raises(IncompatibleActiveExecutions) as caught:
        await ownership.ensure_no_foreign_work(LOCAL)
    assert caught.value.foreign == busy and caught.value.selected is LOCAL
    # The Temporal backend owns its own work: starting it is fine.
    await ownership.ensure_no_foreign_work(TEMPORAL)
    assert active.asked == [TEMPORAL, LOCAL]
    assert other_backend(LOCAL) is TEMPORAL and other_backend(TEMPORAL) is LOCAL


@pytest.mark.asyncio
async def test_queued_requests_alone_also_block() -> None:
    ownership = ExecutionOwnership(FakeActive({LOCAL: ForeignExecutions(LOCAL, 0, 3)}))
    with pytest.raises(IncompatibleActiveExecutions):
        await ownership.ensure_no_foreign_work(TEMPORAL)


def test_temporal_work_message_is_actionable() -> None:
    error = IncompatibleActiveExecutions(
        LOCAL, ForeignExecutions(TEMPORAL, 7, 1, ("r1", "r2", "r3", "r4", "r5"))
    )
    text = execution.describe_foreign_work(error)
    assert "7 active investigation(s) (runs: r1, r2, r3, r4, r5 and 2 more)" in text
    assert "1 queued request(s)" in text
    assert "--execution-backend temporal" in text
    assert "analytics cancel RUN_ID" in text
    assert "EXECUTION_BACKEND=temporal" in text
    assert "Nothing was changed" in text


def test_local_work_message_is_actionable() -> None:
    error = IncompatibleActiveExecutions(
        TEMPORAL, ForeignExecutions(LOCAL, 1, 0, ("r1",))
    )
    text = execution.describe_foreign_work(error)
    assert "belong to local execution" in text
    assert "--execution-backend local" in text
    assert "marked interrupted" in text


def test_selected_scheduler_follows_the_setting() -> None:
    local = api.selected_scheduler(BackendSettings())
    temporal = api.selected_scheduler(BackendSettings(execution_backend=TEMPORAL))
    assert local is not temporal
    # Not entered: building a provider contacts nothing.
    persistence = object.__new__(Persistence)
    assert hasattr(local(persistence), "__aenter__")


def _unreachable_settings() -> BackendSettings:
    from pydantic import SecretStr

    return BackendSettings(
        database_url=SecretStr("postgresql+psycopg://u:p@127.0.0.1:9/none"),
        auth_signing_key=SecretStr("k" * 40),
    )


def test_startup_errors_are_printed_and_fail_the_lifespan(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_foreign_work(*_args: object) -> None:
        return None

    monkeypatch.setattr(api, "ensure_owned_work", no_foreign_work)
    refuse: bool = True

    @asynccontextmanager
    async def refusing(_persistence: Persistence) -> AsyncIterator[object]:
        if refuse:
            raise execution.ExecutionStartupError(execution.LOCK_HELD_MESSAGE)
        yield object()  # pragma: no cover

    app = api.build_app(_unreachable_settings(), scheduler=refusing)  # type: ignore[arg-type]
    with pytest.raises(execution.ExecutionStartupError), TestClient(app):
        pass
    err = capsys.readouterr().err
    assert "retail-analytics-api: cannot start: another API process" in err


def test_evaluation_target_records_the_configured_backend() -> None:
    plans = {"q": [{"answer": {"text": "x", "cite": []}}]}
    from retail_analytics.adapters.models.scripted import scripted_model

    model = scripted_model(plans)
    default = AgentRuntimeTarget(
        BackendSettings(), heldout_source(REPO / "evaluation"), model
    )
    assert default.backend is LOCAL
    assert default.target_id == "agent_runtime:local"
    configured = AgentRuntimeTarget(
        BackendSettings(execution_backend=TEMPORAL),
        heldout_source(REPO / "evaluation"),
        model,
    )
    assert configured.target_id == "agent_runtime:temporal"
    explicit = AgentRuntimeTarget(
        BackendSettings(), heldout_source(REPO / "evaluation"), model, backend=TEMPORAL
    )
    assert explicit.backend is TEMPORAL
    assert explicit.target_id == "agent_runtime:temporal"


_BLOCKED = (
    "temporalio",
    "pydantic_ai.durable_exec.temporal",
    "retail_analytics.adapters.temporal",
    "retail_analytics.bootstrap.temporal",
)

_API_PROBE = r"""
import asyncio, importlib.abc, json, sys

src, blocked = sys.argv[1], tuple(json.loads(sys.argv[2]))
sys.path[:0] = [src]
refused = []

class Blocked(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if any(name == b or name.startswith(b + ".") for b in blocked):
            refused.append(name)
            raise ImportError("blocked: " + name)
        return None

sys.meta_path.insert(0, Blocked())

from pydantic import SecretStr
from retail_analytics.bootstrap import api
from retail_analytics.bootstrap.config import BackendSettings
from retail_analytics.bootstrap.persistence import persistence_from_settings

settings = BackendSettings(
    database_url=SecretStr("postgresql+psycopg://u:p@127.0.0.1:9/none"),
    auth_signing_key=SecretStr("k" * 40),
    telemetry_enabled=False,
)
api.build_app(settings)
out = {}

async def main():
    # The whole local composition is built; opening the manager is the
    # first database contact, which fails here (nothing listens).
    persistence = persistence_from_settings(settings)
    try:
        async with api.selected_scheduler(settings)(persistence):
            out["opened"] = True
    except Exception as error:
        out["failed_at"] = type(error).__name__
    finally:
        persistence.close()

asyncio.run(main())
out["loaded"] = sorted(
    m for m in sys.modules if any(m == b or m.startswith(b + ".") for b in blocked)
)
out["refused"] = sorted(set(refused))
print(json.dumps(out))
"""


def test_local_api_startup_path_never_imports_temporal() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            _API_PROBE,
            str(REPO / "src"),
            json.dumps(_BLOCKED),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env={"PYDANTIC_AI_NO_BANNER": "1"},
    )
    assert completed.returncode == 0, completed.stderr[-4000:]
    report = json.loads(completed.stdout.strip().splitlines()[-1])
    assert report["loaded"] == [] and report["refused"] == []
    assert report["failed_at"] == "OperationalError"


def test_foreign_work_is_checked_before_the_backend_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[str] = []

    async def foreign(_persistence: Persistence, backend: ExecutionBackend) -> None:
        raise execution.ExecutionStartupError(f"busy for {backend.value}")

    @asynccontextmanager
    async def provider(_persistence: Persistence) -> AsyncIterator[object]:
        opened.append("opened")
        yield object()

    monkeypatch.setattr(api, "ensure_owned_work", foreign)
    app = api.build_app(_unreachable_settings(), scheduler=provider)  # type: ignore[arg-type]
    busy = pytest.raises(execution.ExecutionStartupError, match="busy for local")
    with busy, TestClient(app):
        pass
    assert opened == []
