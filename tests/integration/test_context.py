"""Context selection, topic resets and the output gate against real PostgreSQL.

Needs Docker (``pytest -m docker``); runs against a throwaway Compose project.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import psycopg
import pytest

from retail_analytics.application.authorization import (
    AccessDenied,
    ExecutiveRegistration,
    Principal,
)
from retail_analytics.application.output_privacy import (
    OutputDestination,
    OutputSection,
    OutputWithheld,
)
from retail_analytics.application.persistence import (
    IdempotencyConflict,
    OperationRequest,
    RunRequest,
)
from retail_analytics.application.tools.context import OperationContext
from retail_analytics.bootstrap.access import build_access
from retail_analytics.bootstrap.context import ContextServices, build_context
from retail_analytics.bootstrap.evidence import build_evidence
from retail_analytics.bootstrap.persistence import Persistence, build_persistence
from retail_analytics.bootstrap.preferences import build_preferences
from retail_analytics.domain.access import Permission, Role
from retail_analytics.domain.conversation import MessageRole
from retail_analytics.domain.operations import SideEffect
from retail_analytics.domain.runs import RunStatus
from tests.integration.compose_stack import Stack, running_stack
from tests.unit.evidence.support import basis, compiled_and_released

pytestmark = [pytest.mark.docker, pytest.mark.asyncio]
SCOPES = frozenset(p.value for p in Permission)


@pytest.fixture(scope="module")
def stack() -> Iterator[Stack]:
    with running_stack("postgres") as stack:
        stack.migrate()
        yield stack


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class Env:
    def __init__(self, stack: Stack) -> None:
        self.db: Persistence = build_persistence(stack.app_url)
        self.access = build_access(self.db, verifier=None)  # type: ignore[arg-type]
        self.evidence = build_evidence(self.db)
        self.context: ContextServices = build_context(
            self.db,
            self.access,
            self.evidence,
            build_preferences(self.db, self.access),
        )

    async def executive(self, products: set[str]) -> tuple[Principal, str]:
        executive_id = _id("exec")
        await self.db.access_admin.register_executive(
            ExecutiveRegistration(
                executive_id=executive_id,
                issuer="iss",
                subject=f"sub-{executive_id}",
                roles=frozenset({Role.EXECUTIVE}),
                label="Test executive",
            )
        )
        await self.db.access_admin.replace_products(executive_id, products)
        session = await self.db.sessions.create_session(_id("ses"), executive_id)
        return Principal(executive_id, SCOPES), session.session_id

    async def run(self, principal: Principal, session_id: str, text: str) -> str:
        active = await self.db.runs.active_run(session_id)
        if active is not None:
            await self.db.runs.transition_run(active.run_id, RunStatus.COMPLETED)
        started = await self.db.runs.start_run(
            RunRequest(
                run_id=_id("run"),
                session_id=session_id,
                requested_by=principal.executive_id,
                submission_key=_id("key"),
                message_id=_id("msg"),
                request_text=text,
            )
        )
        return started.run.run_id

    async def query(self, principal: Principal, run_id: str) -> tuple[str, list[str]]:
        ctx = await self.access.resolver.context_for_run(principal, run_id)
        op = await self.db.tool_executions.begin(
            OperationRequest(
                operation_id=_id("op"),
                run_id=run_id,
                capability="execute_analysis",
                capability_version=1,
                side_effect=SideEffect.EXTERNAL_JOB,
            )
        )
        compiled, released = compiled_and_released(ctx.executive_id, ctx.product_scope)
        evidence = await self.evidence.record_query(
            OperationContext(ctx, op.execution.operation_id),
            compiled,
            released,
            basis(),
        )
        return evidence.evidence_id, [
            str(r["customer_ref"]) for r in released.records()
        ]


@pytest.fixture
def env(stack: Stack) -> Iterator[Env]:
    e = Env(stack)
    yield e
    e.db.close()


async def test_revocation_resets_and_gate_with_real_persistence(
    env: Env, stack: Stack
) -> None:
    alice, session = await env.executive({"1", "2", "3"})
    r1 = await env.run(alice, session, "Top customers by spend")
    evidence_id, refs = await env.query(alice, r1)
    await env.db.sessions.append_message(
        message_id=_id("msg"),
        session_id=session,
        role=MessageRole.ASSISTANT,
        content=f"{refs[0]} spent the most",
        run_id=r1,
    )
    builder, gate = env.context.builder, env.context.gate

    ctx = await builder.build(alice, r1, "Top customers by spend")
    assert [e.evidence_id for e in ctx.evidence] == [evidence_id]
    assert refs[0] in ctx.render()
    (released,) = await gate.check(
        alice,
        r1,
        [OutputSection("answer", f"{refs[0]} led", (evidence_id,))],
        OutputDestination.REPORT,
    )
    assert released.masked == ()

    # Entitlements narrow: earlier facts leave context and cannot be output.
    await env.db.access_admin.replace_products(alice.executive_id, {"1", "3"})
    r2 = await env.run(alice, session, "And now?")
    ctx = await builder.build(alice, r2, "And now?")
    assert ctx.evidence == () and refs[0] not in ctx.render()
    assert ctx.omissions.history_access_changed == 1
    with pytest.raises(OutputWithheld):
        await gate.check(
            alice,
            r2,
            [OutputSection("answer", f"{refs[0]} led", (evidence_id,))],
            OutputDestination.DISPLAY,
        )

    # Topic reset: durable, idempotent, owner-only, and deletes nothing.
    reset = await builder.reset_topic(alice, session, "reset-1")
    assert await builder.reset_topic(alice, session, "reset-1") == reset
    assert await env.context.resets.latest_reset(session) == reset
    after = await builder.build(alice, r2, "Fresh question about brands")
    assert after.history == () and after.topic_reset_at == reset.reset_at
    bob, bob_session = await env.executive({"2"})
    with pytest.raises(AccessDenied):
        await builder.reset_topic(bob, session, "reset-2")
    with pytest.raises(IdempotencyConflict):
        await env.context.resets.record_reset(bob_session, "reset-1")
    with psycopg.connect(stack.app_dsn) as conn:
        assert conn.execute(
            "select count(*) from evidence where evidence_id = %s", (evidence_id,)
        ).fetchone() == (1,)
        assert conn.execute(
            "select count(*) from messages where session_id = %s", (session,)
        ).fetchone() == (3,)
