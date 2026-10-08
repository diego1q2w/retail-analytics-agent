"""Authorization rules with in-memory stores (PostgreSQL versions: integration)."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

import pytest

from retail_analytics.application.authentication import (
    AuthenticationFailed,
    Authenticator,
    AuthFailure,
)
from retail_analytics.application.authorization import (
    AccessDenied,
    AccessResolver,
    OwnershipGuard,
    require_owner,
)
from retail_analytics.application.contracts.authentication import VerifiedToken
from retail_analytics.application.contracts.authorization import Principal
from retail_analytics.application.tools.registry import (
    AuthorizationSpec,
    CapabilityRegistry,
)
from retail_analytics.domain.access import (
    ExecutiveAccess,
    Permission,
    Role,
    is_valid_product_id,
    permissions_for,
)
from retail_analytics.domain.conversation import Session
from retail_analytics.domain.executions import ToolExecution, ToolExecutionStatus
from retail_analytics.domain.operations import SideEffect
from retail_analytics.domain.runs import Run, RunStatus
from tests.unit.tools.fakes import SQL, Calls, sql_spec

NOW = datetime(2026, 10, 8, tzinfo=UTC)
ALL_SCOPES = frozenset(p.value for p in Permission)


def access(executive_id: str, products: set[str], **kw: object) -> ExecutiveAccess:
    values: dict[str, object] = {
        "roles": frozenset({Role.EXECUTIVE}),
        "active": True,
        "authorization_version": 1,
        **kw,
    }
    return ExecutiveAccess(
        executive_id=executive_id,
        product_ids=frozenset(products),
        **values,  # type: ignore[arg-type]
    )


@dataclass
class Directory:
    by_id: dict[str, ExecutiveAccess] = field(default_factory=dict)
    subjects: dict[tuple[str, str], str] = field(default_factory=dict)

    async def find_by_subject(
        self, issuer: str, subject: str
    ) -> ExecutiveAccess | None:
        executive_id = self.subjects.get((issuer, subject))
        return None if executive_id is None else self.by_id.get(executive_id)

    async def get(self, executive_id: str) -> ExecutiveAccess | None:
        return self.by_id.get(executive_id)


@dataclass
class Records:
    sessions: dict[str, Session] = field(default_factory=dict)
    runs: dict[str, Run] = field(default_factory=dict)
    operations: dict[str, ToolExecution] = field(default_factory=dict)

    async def get_session(self, session_id: str) -> Session | None:
        return self.sessions.get(session_id)

    async def get_run(self, run_id: str) -> Run | None:
        return self.runs.get(run_id)

    async def get(self, operation_id: str) -> ToolExecution | None:
        return self.operations.get(operation_id)

    def add(self, executive_id: str, suffix: str) -> None:
        self.sessions[f"s-{suffix}"] = Session(f"s-{suffix}", executive_id, NOW, NOW)
        self.runs[f"r-{suffix}"] = Run(
            run_id=f"r-{suffix}",
            session_id=f"s-{suffix}",
            requested_by=executive_id,
            trigger_message_id=f"m-{suffix}",
            submission_key=f"k-{suffix}",
            status=RunStatus.RUNNING,
            created_at=NOW,
            updated_at=NOW,
        )
        self.operations[f"op-{suffix}"] = ToolExecution(
            operation_id=f"op-{suffix}",
            run_id=f"r-{suffix}",
            capability="execute_analysis",
            capability_version=1,
            side_effect=SideEffect.EXTERNAL_JOB,
            status=ToolExecutionStatus.PREPARED,
            attempt_count=0,
            created_at=NOW,
            updated_at=NOW,
        )


class Verifier:
    def __init__(self, subject: str, scopes: frozenset[str] = ALL_SCOPES) -> None:
        self.subject = subject
        self.scopes = scopes

    def verify(self, token: str) -> VerifiedToken:
        if token != "good":
            raise AuthenticationFailed(AuthFailure.BAD_SIGNATURE)
        return VerifiedToken("iss", self.subject, self.scopes, NOW)


@pytest.fixture
def world() -> tuple[Directory, Records, AccessResolver, OwnershipGuard]:
    directory = Directory()
    directory.by_id = {
        "exec-a": access("exec-a", {"1", "2"}),
        "exec-b": access("exec-b", {"3"}, roles=frozenset({Role.EXECUTIVE})),
    }
    directory.subjects = {("iss", "sub-a"): "exec-a", ("iss", "sub-b"): "exec-b"}
    records = Records()
    records.add("exec-a", "a")
    records.add("exec-b", "b")
    guard = OwnershipGuard(records, records, records)
    return directory, records, AccessResolver(directory, guard), guard


A = Principal("exec-a", ALL_SCOPES)
B = Principal("exec-b", ALL_SCOPES)


@pytest.mark.asyncio
async def test_authentication_maps_a_verified_subject_to_an_active_executive(
    world: tuple[Directory, Records, AccessResolver, OwnershipGuard],
) -> None:
    directory, *_ = world
    principal = await Authenticator(Verifier("sub-a"), directory).authenticate("good")
    assert principal == A

    with pytest.raises(AuthenticationFailed) as bad:
        await Authenticator(Verifier("sub-a"), directory).authenticate("forged")
    assert bad.value.reason is AuthFailure.BAD_SIGNATURE

    for subject in ("sub-unknown", "sub-b"):
        if subject == "sub-b":
            directory.by_id["exec-b"] = replace(directory.by_id["exec-b"], active=False)
        with pytest.raises(AuthenticationFailed) as unknown:
            await Authenticator(Verifier(subject), directory).authenticate("good")
        assert unknown.value.reason is AuthFailure.UNKNOWN_IDENTITY


@pytest.mark.asyncio
async def test_context_comes_from_server_state_and_token_scopes_only_narrow(
    world: tuple[Directory, Records, AccessResolver, OwnershipGuard],
) -> None:
    _, _, resolver, _ = world
    context = await resolver.context_for_run(A, "r-a", trace_id="t-1")
    assert context.executive_id == "exec-a"
    assert context.product_scope.product_ids == {"1", "2"}
    assert context.product_scope.entitlement_version == 1
    assert context.permissions == permissions_for(frozenset({Role.EXECUTIVE}))
    assert (context.correlation.session_id, context.correlation.run_id) == (
        "s-a",
        "r-a",
    )

    # A token claiming admin scope gains nothing the server did not grant...
    claims_admin = Principal("exec-a", frozenset({"access:admin", "analysis:read"}))
    narrowed = await resolver.context_for_run(claims_admin, "r-a")
    assert narrowed.permissions == {"analysis:read"}
    # ...and a token without scopes can do nothing.
    assert (
        await resolver.context_for_run(Principal("exec-a", frozenset()), "r-a")
    ).permissions == frozenset()

    with pytest.raises(AccessDenied):
        await resolver.require_permission(A, Permission.ACCESS_ADMIN)
    assert await resolver.effective_permissions(claims_admin) == {"analysis:read"}


@pytest.mark.asyncio
async def test_entitlement_changes_are_visible_to_the_next_check(
    world: tuple[Directory, Records, AccessResolver, OwnershipGuard],
) -> None:
    directory, _, resolver, _ = world
    before = await resolver.context_for_run(A, "r-a")
    directory.by_id["exec-a"] = replace(
        directory.by_id["exec-a"], product_ids=frozenset(), authorization_version=2
    )
    after = await resolver.context_for_run(A, "r-a")
    assert before.product_scope.entitlement_version == 1
    assert after.product_scope.entitlement_version == 2
    assert after.product_scope.is_empty

    directory.by_id["exec-a"] = replace(directory.by_id["exec-a"], active=False)
    with pytest.raises(AccessDenied):
        await resolver.context_for_run(A, "r-a")


@pytest.mark.asyncio
async def test_other_executives_records_look_exactly_like_missing_ones(
    world: tuple[Directory, Records, AccessResolver, OwnershipGuard],
) -> None:
    _, _, resolver, guard = world
    assert (await guard.session("exec-a", "s-a")).executive_id == "exec-a"
    assert (await guard.operation("exec-a", "op-a")).run_id == "r-a"

    for kind, key in (("session", "s-b"), ("run", "r-b"), ("operation", "op-b")):
        lookup = getattr(guard, kind)
        with pytest.raises(AccessDenied) as foreign:
            await lookup("exec-a", key)
        with pytest.raises(AccessDenied) as missing:
            await lookup("exec-a", key + "-missing")
        assert (foreign.value.kind, type(foreign.value)) == (
            missing.value.kind,
            type(missing.value),
        )
    with pytest.raises(AccessDenied):
        await resolver.context_for_run(A, "r-b")


@pytest.mark.asyncio
async def test_run_in_someone_elses_session_is_not_owned(
    world: tuple[Directory, Records, AccessResolver, OwnershipGuard],
) -> None:
    _, records, _, guard = world
    records.runs["r-x"] = replace(
        records.runs["r-b"], run_id="r-x", requested_by="exec-a"
    )
    with pytest.raises(AccessDenied):
        await guard.run("exec-a", "r-x")


def test_require_owner_and_role_rules() -> None:
    require_owner("report", "rep-1", "exec-a", "exec-a")
    with pytest.raises(AccessDenied):
        require_owner("report", "rep-1", "exec-a", "exec-b")
    with pytest.raises(AccessDenied):
        require_owner("report", "rep-1", "", "")
    # Admin manages access but gets no analysis rights or products by role.
    admin = access("exec-admin", set(), roles=frozenset({Role.ADMIN}))
    assert admin.permissions == {Permission.ACCESS_ADMIN}
    assert admin.product_scope.is_empty
    disabled = access("exec-a", {"1"}, active=False)
    assert disabled.permissions == frozenset()
    assert disabled.product_scope.is_empty


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("1", True),
        ("29120", True),
        ("0", False),
        ("01", False),
        ("-1", False),
        ("1e3", False),
        ("", False),
        ("\u0661", False),
        ("1" * 20, False),
    ],
)
def test_product_id_shape(value: str, ok: bool) -> None:
    assert is_valid_product_id(value) is ok


@pytest.mark.asyncio
async def test_empty_entitlements_hide_data_tools_from_the_resolved_context(
    world: tuple[Directory, Records, AccessResolver, OwnershipGuard],
) -> None:
    directory, _, resolver, _ = world
    analysis = replace(
        sql_spec(Calls()),
        authorization=AuthorizationSpec(
            required_permissions=frozenset({Permission.ANALYSIS_READ.value}),
            requires_product_scope=True,
        ),
    )
    registry = CapabilityRegistry([analysis])
    entitled = await resolver.context_for_run(A, "r-a")
    assert [tool.name for tool in registry.catalog(entitled)] == [SQL]

    directory.by_id["exec-a"] = replace(
        directory.by_id["exec-a"], product_ids=frozenset(), authorization_version=2
    )
    empty = await resolver.context_for_run(A, "r-a")
    assert registry.catalog(empty) == ()
    assert registry.resolve(SQL, empty) is None
