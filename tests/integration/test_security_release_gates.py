"""Release-time authority recheck through the real runtime, local backend (T38).

PostgreSQL only (the default local execution mode): an answer drafted under
one entitlement is released by ``InvestigationRuntime.release_answer`` after
an administrator narrowed the executive's products. The manager is not
started; the test plays the model's part so the revocation lands exactly
between model completion and release.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from pydantic_ai.models.function import FunctionModel

from retail_analytics.adapters.postgres.database import Database
from retail_analytics.adapters.postgres.investigations import PostgresRunPrincipals
from retail_analytics.application.contracts.authorization import Principal
from retail_analytics.application.contracts.investigations import (
    AnswerDraft,
    StepResult,
)
from retail_analytics.application.contracts.persistence import RunRequest
from retail_analytics.bootstrap.config import BackendSettings
from retail_analytics.bootstrap.local_investigations import (
    LocalInvestigations,
    build_local_investigations,
)
from retail_analytics.domain.runs import ExecutionBackend
from tests.integration.compose_stack import Stack, running_stack
from tests.integration.scripted_investigations import scripted_model
from tests.integration.test_context import Env

pytestmark = [pytest.mark.docker, pytest.mark.asyncio]

# Uncited and free of any figure the numeric check can recognise.
DERIVED = "Your strongest product is up 12% on 7 orders."


@pytest.fixture(scope="module")
def stack() -> Iterator[Stack]:
    with running_stack("postgres") as stack:
        stack.migrate()
        yield stack


class ReleaseEnv(Env):
    __test__ = False
    principal: Principal
    session_id: str
    local: LocalInvestigations

    async def run(self, principal: Principal, session_id: str, text: str) -> str:
        run_id = "run-" + uuid.uuid4().hex[:12]
        # What the launcher records before scheduling (the manager is not run).
        await PostgresRunPrincipals(Database(self.db.engine)).record(run_id, principal)
        started = await self.db.runs.start_run(
            RunRequest(
                run_id=run_id,
                session_id=session_id,
                requested_by=principal.executive_id,
                submission_key=uuid.uuid4().hex,
                message_id="msg-" + uuid.uuid4().hex[:12],
                request_text=text,
                execution_backend=ExecutionBackend.LOCAL,
            )
        )
        assert started.run.run_id == run_id
        await self.local.services.runtime.begin(run_id)
        return run_id

    async def answer(self, run_id: str, text: str, *cited: str) -> StepResult:
        outcome = await self.local.services.runtime.release_answer(
            AnswerDraft(run_id, 1, text, tuple(cited))
        )
        return outcome.result

    async def assistant_texts(self) -> list[str]:
        messages = await self.db.sessions.recent_messages(self.session_id, 200)
        return [m.content for m in messages if m.role.value == "assistant"]


@pytest_asyncio.fixture
async def env(stack: Stack) -> AsyncIterator[ReleaseEnv]:
    env = ReleaseEnv(stack)
    env.local = build_local_investigations(
        BackendSettings(), env.db, env.access, FunctionModel(scripted_model)
    )
    try:
        yield env
    finally:
        env.db.close()


async def _fresh_session(env: ReleaseEnv) -> None:
    env.principal, env.session_id = await env.executive({"1"})


async def test_answer_released_under_unchanged_access(env: ReleaseEnv) -> None:
    await _fresh_session(env)
    run_id = await env.run(env.principal, env.session_id, "Revenue for my products")
    evidence_id, _ = await env.query(env.principal, run_id)
    assert await env.answer(run_id, DERIVED, evidence_id) is StepResult.RELEASED
    assert any(DERIVED in t for t in await env.assistant_texts())


async def test_cited_answer_is_withheld_after_mid_generation_revocation(
    env: ReleaseEnv,
) -> None:
    await _fresh_session(env)
    run_id = await env.run(env.principal, env.session_id, "Revenue for my products")
    evidence_id, _ = await env.query(env.principal, run_id)
    await env.db.access_admin.replace_products(env.principal.executive_id, {"2"})
    result = await env.answer(run_id, DERIVED, evidence_id)
    assert result in {StepResult.WITHHELD, StepResult.STOPPED}
    assert not any(DERIVED in t for t in await env.assistant_texts())


@pytest.mark.xfail(
    strict=True,
    reason=(
        "RELEASE BLOCKER G-1: an uncited conclusion resting on run evidence "
        "revoked mid-generation is released (only cited ids and recognisable "
        "figures are rechecked)."
    ),
)
async def test_uncited_answer_is_withheld_after_mid_generation_revocation(
    env: ReleaseEnv,
) -> None:
    await _fresh_session(env)
    run_id = await env.run(env.principal, env.session_id, "Revenue for my products")
    await env.query(env.principal, run_id)
    await env.db.access_admin.replace_products(env.principal.executive_id, {"2"})
    result = await env.answer(run_id, DERIVED)
    assert result in {StepResult.WITHHELD, StepResult.STOPPED}
