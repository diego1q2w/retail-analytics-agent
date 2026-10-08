"""Evidence store and guarded reuse against real PostgreSQL.

Needs Docker (``pytest -m docker``); runs against a throwaway Compose project.
Released results come from the DuckDB privacy oracle, compiled for the
executive's actual product scope and authorization version in the database.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import timedelta

import psycopg
import pytest

from retail_analytics.application.authorization import AccessDenied
from retail_analytics.application.contracts.authorization import (
    ExecutiveRegistration,
    Principal,
)
from retail_analytics.application.contracts.persistence import (
    IdempotencyConflict,
    OperationRequest,
    RunRequest,
)
from retail_analytics.application.contracts.query_compiler import CompiledQuery
from retail_analytics.application.contracts.tools import (
    ExecutionContext,
    OperationContext,
)
from retail_analytics.application.evidence import (
    EvidenceRejected,
    EvidenceService,
    ReuseRequest,
    query_subject_key,
)
from retail_analytics.bootstrap.access import AccessServices, build_access
from retail_analytics.bootstrap.evidence import build_evidence
from retail_analytics.bootstrap.persistence import Persistence, build_persistence
from retail_analytics.bootstrap.preferences import build_preferences
from retail_analytics.domain.access import Permission, Role
from retail_analytics.domain.evidence import (
    AnalysisStamp,
    Evidence,
    EvidenceColumn,
    EvidenceContent,
    EvidenceKind,
    EvidenceTable,
    EvidenceUse,
    PinHolder,
    Provenance,
    ReuseBlock,
    ReuseIntent,
)
from retail_analytics.domain.operations import SideEffect
from retail_analytics.domain.preferences import PreferenceSetting
from retail_analytics.domain.runs import RunStatus
from tests.integration.compose_stack import Stack, running_stack
from tests.unit.evidence.fakes import Clock
from tests.unit.evidence.support import (
    FINGERPRINT,
    REVENUE,
    SEPTEMBER,
    basis,
    compiled_and_released,
    requirements,
)

pytestmark = [pytest.mark.docker, pytest.mark.asyncio]
SCOPES = frozenset(p.value for p in Permission)
PRODUCTS = {"1", "3"}


@pytest.fixture(scope="module")
def stack() -> Iterator[Stack]:
    with running_stack("postgres") as stack:
        stack.migrate()
        yield stack


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class Env:
    def __init__(self, stack: Stack) -> None:
        self.stack = stack
        self.clock = Clock()
        self.db: Persistence = build_persistence(stack.app_url)
        self.access: AccessServices = build_access(self.db, verifier=None)  # type: ignore[arg-type]
        self.service: EvidenceService = build_evidence(self.db, clock=self.clock)

    async def executive(self, products: set[str] = PRODUCTS) -> tuple[Principal, str]:
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

    async def run(self, principal: Principal, session_id: str) -> ExecutionContext:
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
                request_text="Top customers by spend in September",
            )
        )
        return await self.access.resolver.context_for_run(principal, started.run.run_id)

    async def operation(self, ctx: ExecutionContext) -> OperationContext:
        op = await self.db.tool_executions.begin(
            OperationRequest(
                operation_id=_id("op"),
                run_id=ctx.correlation.run_id,
                capability="execute_analysis",
                capability_version=1,
                side_effect=SideEffect.EXTERNAL_JOB,
            )
        )
        return OperationContext(ctx, op.execution.operation_id)

    async def record(
        self, ctx: ExecutionContext, *, refreshes: str | None = None
    ) -> tuple[CompiledQuery, Evidence]:
        compiled, released = compiled_and_released(ctx.executive_id, ctx.product_scope)
        evidence = await self.service.record_query(
            await self.operation(ctx), compiled, released, basis(), refreshes=refreshes
        )
        return compiled, evidence


def _current(compiled: CompiledQuery, **changes: object) -> ReuseRequest:
    return ReuseRequest(
        ReuseIntent.CURRENT,
        requirements(compiled, **changes),
        subject_key=query_subject_key(compiled),
    )


@pytest.fixture
def env(stack: Stack) -> Iterator[Env]:
    e = Env(stack)
    yield e
    e.db.close()


async def test_records_are_immutable_idempotent_and_hold_no_secrets(
    env: Env, stack: Stack
) -> None:
    alice, session = await env.executive()
    ctx = await env.run(alice, session)
    compiled, released = compiled_and_released(ctx.executive_id, ctx.product_scope)
    op = await env.operation(ctx)

    first, second = await asyncio.gather(
        env.service.record_query(op, compiled, released, basis()),
        env.service.record_query(op, compiled, released, basis()),
    )
    assert first == second
    loaded = await env.db.evidence.get(first.evidence_id)
    assert loaded is not None and loaded.evidence == first and loaded.evidence.is_intact
    assert loaded.evidence.content.table.rows == released.rows
    with pytest.raises(IdempotencyConflict):
        await env.service.record_query(
            op, compiled, released, basis(preference_fingerprint="other")
        )

    secrets = [str(p.value) for p in compiled.parameters if p.secret]
    assert secrets
    with psycopg.connect(stack.app_dsn) as conn:
        stored = conn.execute(
            "select provenance::text || payload::text || analysis::text "
            "from evidence where evidence_id = %s",
            (first.evidence_id,),
        ).fetchone()
        assert stored is not None
        text = stored[0]
        assert "_policy_" not in text and compiled.sql not in text
        assert not any(secret in text for secret in secrets)
        assert conn.execute(
            "select count(*) from evidence where operation_id = %s",
            (op.operation_id,),
        ).fetchone() == (1,)
        with pytest.raises(psycopg.errors.RestrictViolation):
            conn.execute(
                "update evidence set payload = '{}'::jsonb where evidence_id = %s",
                (first.evidence_id,),
            )


async def test_follow_up_reuse_freshness_and_refresh_versions(env: Env) -> None:
    alice, session = await env.executive()
    first_run = await env.run(alice, session)
    compiled, old = await env.record(first_run)

    env.clock.advance(timedelta(minutes=5))
    second_run = await env.run(alice, session)
    outcome = await env.service.find_reusable(second_run, _current(compiled))
    assert outcome.reused is not None and outcome.reused.evidence == old
    links = await env.db.evidence.for_run(second_run.correlation.run_id)
    assert [(x.evidence_id, x.use) for x in links] == [
        (old.evidence_id, EvidenceUse.REUSED)
    ]

    env.clock.advance(timedelta(minutes=11))
    third_run = await env.run(alice, session)
    stale = await env.service.find_reusable(third_run, _current(compiled))
    assert stale.reused is None
    assert stale.considered == ((old.evidence_id, ReuseBlock.STALE),)
    explained = await env.service.find_reusable(
        third_run,
        ReuseRequest(
            ReuseIntent.EXPLAIN, requirements(compiled), evidence_id=old.evidence_id
        ),
    )
    assert explained.reused is not None
    assert explained.reused.snapshot.age == timedelta(minutes=16)

    _, new = await env.record(third_run, refreshes=stale.refresh_of)
    assert (new.lineage_id, new.version) == (old.lineage_id, 2)
    unchanged = await env.db.evidence.get(old.evidence_id)
    assert unchanged is not None and unchanged.evidence == old
    fresh = await env.service.find_reusable(third_run, _current(compiled))
    assert fresh.reused is not None and fresh.reused.evidence == new


async def test_entitlement_change_and_other_executives_block_reuse(env: Env) -> None:
    alice, session = await env.executive()
    bob, bob_session = await env.executive({"2"})
    ctx = await env.run(alice, session)
    compiled, evidence = await env.record(ctx)

    bob_ctx = await env.run(bob, bob_session)
    denied = await env.service.find_reusable(
        bob_ctx,
        ReuseRequest(
            ReuseIntent.EXPLAIN,
            requirements(compiled),
            evidence_id=evidence.evidence_id,
        ),
    )
    assert denied.reused is None and denied.considered == ()
    with pytest.raises(AccessDenied):
        await env.service.pin_for(
            bob.executive_id, [evidence.evidence_id], PinHolder("report", "r")
        )

    await env.db.access_admin.replace_products(alice.executive_id, {"1"})
    narrowed = await env.access.resolver.context_for_run(alice, ctx.correlation.run_id)
    blocked = await env.service.find_reusable(narrowed, _current(compiled))
    assert blocked.considered == (
        (evidence.evidence_id, ReuseBlock.AUTHORIZATION_CHANGED),
    )
    assert await env.service.usable_in_session(narrowed) == ()
    _, released = compiled_and_released(ctx.executive_id, ctx.product_scope)
    with pytest.raises(EvidenceRejected):
        await env.service.record_query(
            await env.operation(narrowed), compiled, released, basis()
        )


async def test_preference_change_invalidates_dependent_evidence(env: Env) -> None:
    alice, session = await env.executive()
    ctx = await env.run(alice, session)
    compiled, evidence = await env.record(ctx)
    derived = await env.service.record(
        await env.operation(ctx),
        EvidenceContent(
            kind=EvidenceKind.DERIVED,
            subject_key="share",
            analysis=AnalysisStamp(
                compiled.catalog_version,
                1,
                frozenset({REVENUE}),
                FINGERPRINT,
                SEPTEMBER,
            ),
            provenance=Provenance(notes=(("method", "share of total"),)),
            table=EvidenceTable((EvidenceColumn("share", "value"),), ((0.25,),), 1),
            grain=(),
            derived_from=(evidence.evidence_id,),
        ),
    )
    loaded = await env.db.evidence.get(derived.evidence_id)
    assert loaded is not None
    assert loaded.evidence.content.derived_from == (evidence.evidence_id,)

    preferences = build_preferences(env.db, env.access)
    await preferences.remember(
        alice, PreferenceSetting.metric("revenue", "completed_item_sales", 1)
    )
    for evidence_id in (evidence.evidence_id, derived.evidence_id):
        stored = await env.db.evidence.get(evidence_id)
        assert stored is not None and stored.invalidated
    outcome = await env.service.find_reusable(ctx, _current(compiled))
    assert outcome.considered == ((evidence.evidence_id, ReuseBlock.INVALIDATED),)


async def test_pins_keep_report_evidence_from_deletion(env: Env, stack: Stack) -> None:
    alice, session = await env.executive()
    ctx = await env.run(alice, session)
    _, kept = await env.record(ctx)
    _, loose = await env.record(ctx)
    report = PinHolder("report", _id("rep"))
    await env.service.pin_for(alice.executive_id, [kept.evidence_id], report)
    await env.service.pin_for(alice.executive_id, [kept.evidence_id], report)
    assert await env.db.evidence.pinned(report) == (kept.evidence_id,)
    assert await env.db.evidence.holders(kept.evidence_id) == (report,)

    with psycopg.connect(stack.app_dsn) as conn:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                "delete from evidence where evidence_id = %s", (kept.evidence_id,)
            )
        conn.rollback()
        conn.execute(
            "delete from evidence where evidence_id = %s", (loose.evidence_id,)
        )
        conn.commit()
    assert await env.db.evidence.get(loose.evidence_id) is None
    assert await env.service.release_pins(report) == 1
    assert await env.db.evidence.pinned(report) == ()
