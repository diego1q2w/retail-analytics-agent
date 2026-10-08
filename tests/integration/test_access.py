"""Authentication, entitlements and ownership against real PostgreSQL.

Needs Docker (``pytest -m docker``); runs against a throwaway Compose project.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from click.testing import CliRunner

from retail_analytics.adapters.auth.local_jwt import LocalJwtAuthority
from retail_analytics.application.authentication import (
    AuthenticationFailed,
    AuthFailure,
)
from retail_analytics.application.authorization import (
    AccessDenied,
    ExecutiveRegistration,
    Principal,
)
from retail_analytics.application.persistence import (
    IdempotencyConflict,
    OperationRequest,
    RunRequest,
)
from retail_analytics.bootstrap import dev_access
from retail_analytics.bootstrap.access import AccessServices, build_access
from retail_analytics.bootstrap.dev_access import (
    DEMO_EXECUTIVES,
    provision_demo_executives,
)
from retail_analytics.bootstrap.persistence import Persistence, build_persistence
from retail_analytics.domain.access import Permission, Role
from retail_analytics.domain.operations import SideEffect
from tests.integration.compose_stack import Stack, running_stack

pytestmark = [pytest.mark.docker, pytest.mark.asyncio]
ISSUER = "retail-analytics-local"
KEY = "integration-test-signing-key-" + "z" * 40


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


def _authority(**overrides: object) -> LocalJwtAuthority:
    options: dict[str, object] = {"issuer": ISSUER, "audience": "retail-analytics-api"}
    options.update(overrides)
    key = str(options.pop("key", KEY))
    return LocalJwtAuthority(key, **options)  # type: ignore[arg-type]


@pytest.fixture
def services(db: Persistence) -> AccessServices:
    return build_access(db, _authority())


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


async def _register(db: Persistence, products: set[str]) -> str:
    executive_id = _id("exec")
    await db.access_admin.register_executive(
        ExecutiveRegistration(
            executive_id=executive_id,
            issuer=ISSUER,
            subject=f"sub-{executive_id}",
            roles=frozenset({Role.EXECUTIVE}),
            label="Test executive",
        )
    )
    await db.access_admin.replace_products(executive_id, products)
    return executive_id


async def _owned_records(db: Persistence, executive_id: str) -> tuple[str, str, str]:
    session = await db.sessions.create_session(_id("ses"), executive_id)
    started = await db.runs.start_run(
        RunRequest(
            run_id=_id("run"),
            session_id=session.session_id,
            requested_by=executive_id,
            submission_key=_id("key"),
            message_id=_id("msg"),
            request_text="How did revenue change?",
        )
    )
    operation = await db.tool_executions.begin(
        OperationRequest(
            operation_id=_id("op"),
            run_id=started.run.run_id,
            capability="execute_analysis",
            capability_version=1,
            side_effect=SideEffect.EXTERNAL_JOB,
        )
    )
    return (
        session.session_id,
        started.run.run_id,
        operation.execution.operation_id,
    )


async def test_demo_provisioning_is_disjoint_and_idempotent(
    stack: Stack, db: Persistence
) -> None:
    first = await provision_demo_executives(db.access_admin, ISSUER)
    again = await provision_demo_executives(db.access_admin, ISSUER)
    assert [a.executive_id for a in first] == ["exec-demo-a", "exec-demo-b"]
    assert first == again  # a rerun changes nothing, not even the version
    a, b = first
    assert len(a.product_ids) == 15989
    assert len(b.product_ids) == 13131
    assert not a.product_ids & b.product_ids
    assert Permission.PERSONA_EDIT in a.permissions
    assert Permission.KNOWLEDGE_REVIEW in b.permissions
    assert all(Permission.ACCESS_ADMIN not in x.permissions for x in first)
    with psycopg.connect(stack.app_dsn) as conn:
        row = conn.execute("select count(*) from product_entitlements").fetchone()
    assert row is not None
    assert row[0] >= 15989 + 13131


async def test_demo_token_authenticates_and_bad_tokens_do_not(
    db: Persistence, services: AccessServices
) -> None:
    await provision_demo_executives(db.access_admin, ISSUER)
    demo = DEMO_EXECUTIVES[0]
    scopes = {p.value for p in Permission}
    token = _authority().issue(demo.subject, scopes, timedelta(minutes=5))
    principal = await services.authenticator.authenticate(token)
    assert principal.executive_id == "exec-demo-a"

    bad_tokens = {
        AuthFailure.BAD_SIGNATURE: _authority(key="another-key-" + "q" * 40).issue(
            demo.subject, scopes, timedelta(minutes=5)
        ),
        AuthFailure.EXPIRED: _authority(
            clock=lambda: datetime.now(UTC) - timedelta(hours=2)
        ).issue(demo.subject, scopes, timedelta(minutes=5)),
        AuthFailure.WRONG_AUDIENCE: _authority(audience="other-api").issue(
            demo.subject, scopes, timedelta(minutes=5)
        ),
        AuthFailure.WRONG_ISSUER: _authority(issuer="https://other-idp").issue(
            demo.subject, scopes, timedelta(minutes=5)
        ),
        AuthFailure.UNKNOWN_IDENTITY: _authority().issue(
            "nobody", scopes, timedelta(minutes=5)
        ),
    }
    for reason, bad in bad_tokens.items():
        with pytest.raises(AuthenticationFailed) as caught:
            await services.authenticator.authenticate(bad)
        assert caught.value.reason is reason


async def test_executives_cannot_reach_each_others_sessions_runs_or_operations(
    db: Persistence, services: AccessServices
) -> None:
    alice = await _register(db, {"1", "2"})
    bob = await _register(db, {"3"})
    a_session, a_run, a_op = await _owned_records(db, alice)
    b_session, b_run, b_op = await _owned_records(db, bob)
    guard, resolver = services.guard, services.resolver

    assert (await guard.session(alice, a_session)).executive_id == alice
    assert (await guard.run(alice, a_run)).session_id == a_session
    assert (await guard.operation(alice, a_op)).run_id == a_run

    for executive, session, run, op in (
        (alice, b_session, b_run, b_op),
        (bob, a_session, a_run, a_op),
    ):
        with pytest.raises(AccessDenied):
            await guard.session(executive, session)
        with pytest.raises(AccessDenied):
            await guard.run(executive, run)
        with pytest.raises(AccessDenied):
            await guard.operation(executive, op)
        with pytest.raises(AccessDenied):
            await resolver.context_for_run(
                Principal(executive, frozenset({"analysis:read"})), run
            )


async def test_entitlement_updates_bump_the_version_seen_by_fresh_checks(
    db: Persistence, services: AccessServices
) -> None:
    alice = await _register(db, {"10", "11"})
    _, run, _ = await _owned_records(db, alice)
    principal = Principal(alice, frozenset({"analysis:read"}))
    resolver = services.resolver

    before = await resolver.context_for_run(principal, run)
    assert before.product_scope.product_ids == {"10", "11"}
    assert before.permissions == {"analysis:read"}

    unchanged = await db.access_admin.replace_products(alice, {"11", "10"})
    assert unchanged.authorization_version == before.product_scope.entitlement_version

    await db.access_admin.replace_products(alice, {"11", "12"})
    after = await resolver.context_for_run(principal, run)
    assert after.product_scope.product_ids == {"11", "12"}
    assert after.product_scope.entitlement_version == (
        before.product_scope.entitlement_version + 1
    )

    await db.access_admin.replace_products(alice, set())
    empty = await resolver.context_for_run(principal, run)
    assert empty.product_scope.is_empty
    assert empty.product_scope.entitlement_version == (
        after.product_scope.entitlement_version + 1
    )

    disabled = await db.access_admin.set_active(alice, False)
    assert disabled.authorization_version == empty.product_scope.entitlement_version + 1
    with pytest.raises(AccessDenied):
        await resolver.context_for_run(principal, run)
    token = _authority().issue(f"sub-{alice}", {"analysis:read"}, timedelta(minutes=5))
    with pytest.raises(AuthenticationFailed):
        await services.authenticator.authenticate(token)


async def test_concurrent_updates_each_get_their_own_version(db: Persistence) -> None:
    alice = await _register(db, set())
    start = await db.executives.get(alice)
    assert start is not None
    other = build_persistence(db.engine.url.render_as_string(hide_password=False))
    try:
        await asyncio.gather(
            *(db.access_admin.replace_products(alice, {str(i)}) for i in range(1, 6)),
            *(
                other.access_admin.replace_products(alice, {str(i)})
                for i in range(6, 11)
            ),
        )
    finally:
        other.close()
    final = await db.executives.get(alice)
    assert final is not None
    assert final.authorization_version == start.authorization_version + 10
    assert len(final.product_ids) == 1


async def test_identity_and_input_conflicts_are_rejected(db: Persistence) -> None:
    alice = await _register(db, {"5"})
    with pytest.raises(IdempotencyConflict):
        await db.access_admin.register_executive(
            ExecutiveRegistration(
                executive_id=_id("exec"),
                issuer=ISSUER,
                subject=f"sub-{alice}",
                roles=frozenset({Role.EXECUTIVE}),
                label="Impostor",
            )
        )
    with pytest.raises(IdempotencyConflict):
        await db.access_admin.register_executive(
            ExecutiveRegistration(
                executive_id=alice,
                issuer=ISSUER,
                subject="a-different-subject",
                roles=frozenset({Role.EXECUTIVE}),
                label="Test executive",
            )
        )
    with pytest.raises(ValueError, match="invalid product IDs"):
        await db.access_admin.replace_products(alice, {"5", "drop table"})

    promoted = await db.access_admin.register_executive(
        ExecutiveRegistration(
            executive_id=alice,
            issuer=ISSUER,
            subject=f"sub-{alice}",
            roles=frozenset({Role.EXECUTIVE, Role.ADMIN}),
            label="Test executive",
        )
    )
    assert Permission.ACCESS_ADMIN in promoted.permissions
    assert promoted.product_ids == {"5"}
    assert promoted.authorization_version == 3


async def test_dev_command_provisions_and_issues_a_working_token(
    stack: Stack, services: AccessServices
) -> None:
    env = {
        "RETAIL_ANALYTICS_DATABASE_URL": stack.app_url,
        "RETAIL_ANALYTICS_AUTH_SIGNING_KEY": KEY,
    }
    runner = CliRunner()
    provisioned = await asyncio.to_thread(
        runner.invoke, dev_access.main, ["provision"], env=env
    )
    assert provisioned.exit_code == 0, provisioned.output
    assert "exec-demo-b: roles=executive,reviewer products=13131" in provisioned.output
    assert stack.app_password not in provisioned.output

    issued = await asyncio.to_thread(
        runner.invoke, dev_access.main, ["token", "demo-b", "--minutes", "5"], env=env
    )
    assert issued.exit_code == 0, issued.output
    principal = await services.authenticator.authenticate(issued.output.strip())
    assert principal.executive_id == "exec-demo-b"
    assert Permission.ANALYSIS_READ.value in principal.scopes
    assert Permission.ACCESS_ADMIN.value not in principal.scopes
