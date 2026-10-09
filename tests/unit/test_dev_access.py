"""Local admin identity and the local self-publication policy (T41).

The person evaluating the demo is provisioned explicitly (roles and product
grant), may publish their own Golden examples only through the wiring that
names them, and that review is recorded as self-published. Everyone else keeps
the independent-reviewer rule.
"""

from __future__ import annotations

from pathlib import Path

import click
import pytest

from retail_analytics.adapters.filesystem.blobs import LocalBlobStore
from retail_analytics.application.artifacts import (
    ArtifactMaintenance,
    ArtifactPolicy,
    ArtifactService,
)
from retail_analytics.application.authorization import (
    AccessDenied,
    AccessResolver,
    OwnershipGuard,
)
from retail_analytics.application.contracts.authorization import (
    ExecutiveRegistration,
    Principal,
)
from retail_analytics.application.knowledge import (
    SELF_PUBLISHED_CHECK,
    KnowledgeError,
    KnowledgeErrorCode,
    KnowledgeService,
)
from retail_analytics.bootstrap import dev_access, knowledge_admin
from retail_analytics.bootstrap.dev_access import (
    DEMO_EXECUTIVES,
    LOCAL_ADMIN,
    LOCAL_EXECUTIVES,
    provision_demo_executives,
)
from retail_analytics.domain.access import (
    ExecutiveAccess,
    Permission,
    Role,
    permissions_for,
)
from retail_analytics.domain.knowledge import ReviewAction, ReviewStatus
from tests.knowledge_scenarios import OK, draft, key
from tests.unit.memory_knowledge import MemoryKnowledgeRepository
from tests.unit.test_artifacts import MemoryCatalog
from tests.unit.test_knowledge import NOW, _Directory, _NoRecords

ADMIN = LOCAL_ADMIN.executive_id
ALL = frozenset(p.value for p in Permission)


# -- the identity -----------------------------------------------------------


def test_local_admin_has_every_role_and_an_explicit_full_product_grant() -> None:
    assert LOCAL_ADMIN.roles == frozenset(Role)
    assert LOCAL_ADMIN.product_ids == frozenset(str(i) for i in range(1, 29121))
    # Admin status grants no product data: the grant above is what does.
    assert Permission.ANALYSIS_READ not in permissions_for(frozenset({Role.ADMIN}))
    assert not LOCAL_ADMIN.brands


def test_restricted_demo_identities_are_brand_managers_without_explicit_grants() -> (
    None
):
    a, b = DEMO_EXECUTIVES
    assert a.brands and b.brands and a.brands.isdisjoint(b.brands)
    assert not a.product_ids and not b.product_ids


def test_restricted_demo_identities_are_unchanged_and_still_provisioned() -> None:
    assert [d.key for d in DEMO_EXECUTIVES] == ["demo-a", "demo-b"]
    assert (LOCAL_ADMIN, *DEMO_EXECUTIVES) == LOCAL_EXECUTIVES
    assert all(Role.ADMIN not in d.roles for d in DEMO_EXECUTIVES)


class _RecordingAdmin:
    def __init__(self) -> None:
        self.registered: list[ExecutiveRegistration] = []
        self.products: dict[str, frozenset[str]] = {}
        self.brands: dict[str, frozenset[str]] = {}

    async def register_executive(
        self, registration: ExecutiveRegistration, *, actor_id: str
    ) -> ExecutiveAccess:
        assert actor_id == dev_access.DEV_ACTOR
        self.registered.append(registration)
        return self._access(registration.executive_id, registration.roles)

    async def replace_products(
        self, executive_id: str, product_ids: frozenset[str], *, actor_id: str
    ) -> ExecutiveAccess:
        self.products[executive_id] = frozenset(product_ids)
        roles = next(r.roles for r in self.registered if r.executive_id == executive_id)
        return self._access(executive_id, roles)

    async def replace_brands(
        self, executive_id: str, brands: frozenset[str], *, actor_id: str
    ) -> ExecutiveAccess:
        assert actor_id == dev_access.DEV_ACTOR
        self.brands[executive_id] = frozenset(brands)
        roles = next(r.roles for r in self.registered if r.executive_id == executive_id)
        return self._access(executive_id, roles)

    def _access(self, executive_id: str, roles: frozenset[Role]) -> ExecutiveAccess:
        return ExecutiveAccess(
            executive_id=executive_id,
            roles=roles,
            product_ids=self.products.get(executive_id, frozenset()),
            active=True,
            authorization_version=1,
        )


@pytest.mark.asyncio
async def test_provisioning_registers_roles_and_grants_products_explicitly() -> None:
    admin = _RecordingAdmin()
    await provision_demo_executives(
        admin,  # type: ignore[arg-type]
        "issuer",
        LOCAL_EXECUTIVES,
        brands=admin,  # type: ignore[arg-type]
    )
    first = admin.registered[0]
    assert first.executive_id == ADMIN
    assert first.roles == frozenset(Role)
    assert admin.products[ADMIN] == LOCAL_ADMIN.product_ids
    assert admin.brands[ADMIN] == frozenset()
    for demo in DEMO_EXECUTIVES:
        # Older department ranges are cleared; brands are the scope.
        assert admin.products[demo.executive_id] == frozenset()
        assert admin.brands[demo.executive_id] == demo.brands


@pytest.mark.asyncio
async def test_brand_managers_cannot_be_provisioned_without_the_brand_store() -> None:
    with pytest.raises(ValueError, match="brand access store"):
        await provision_demo_executives(_RecordingAdmin(), "issuer")  # type: ignore[arg-type]


def test_token_defaults_to_the_local_admin() -> None:
    token = dev_access.main.commands["token"]
    argument = next(p for p in token.params if p.name == "executive")
    assert argument.default == LOCAL_ADMIN.key
    assert isinstance(argument.type, click.Choice)
    assert set(argument.type.choices) == {"local-admin", "demo-a", "demo-b"}


def test_only_the_local_admin_is_named_by_the_self_publication_policy() -> None:
    assert frozenset({ADMIN}) == knowledge_admin.LOCAL_SELF_PUBLISHERS


# -- the policy -------------------------------------------------------------


def _people() -> dict[str, ExecutiveAccess]:
    def access(name: str, roles: set[Role], products: set[str]) -> ExecutiveAccess:
        return ExecutiveAccess(
            executive_id=name,
            roles=frozenset(roles),
            product_ids=frozenset(products),
            active=True,
            authorization_version=1,
        )

    return {
        ADMIN: access(ADMIN, set(Role), {"1", "2", "3"}),
        "exec-both": access("exec-both", {Role.EXECUTIVE, Role.REVIEWER}, {"1"}),
        "exec-reviewer": access("exec-reviewer", {Role.REVIEWER}, {"1", "2", "3"}),
    }


def _service(tmp_path: Path, self_publishers: frozenset[str]) -> KnowledgeService:
    resolver = AccessResolver(
        _Directory(_people()), OwnershipGuard(_NoRecords(), _NoRecords(), _NoRecords())
    )
    blobs = LocalBlobStore(tmp_path / "artifacts")
    catalog = MemoryCatalog()
    return KnowledgeService(
        resolver,
        MemoryKnowledgeRepository(),
        ArtifactService(catalog, blobs, ArtifactPolicy()),
        ArtifactMaintenance(catalog, blobs),
        clock=lambda: NOW,
        self_publishers=self_publishers,
    )


def _as(executive_id: str) -> Principal:
    return Principal(executive_id, ALL)


@pytest.mark.asyncio
async def test_local_admin_publishes_own_example_audited_as_self_published(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path, knowledge_admin.LOCAL_SELF_PUBLISHERS)
    version = await service.submit_candidate(_as(ADMIN), draft(), idempotency_key=key())
    result = await service.approve(
        _as(ADMIN), version.ref, rationale="Checked locally.", checks=OK
    )
    assert result.version.status is ReviewStatus.PUBLISHED
    assert result.version.author_id == ADMIN  # authorship is never rewritten
    approval = [
        e
        for e in await service.history(_as(ADMIN), version.ref)
        if e.action is ReviewAction.APPROVE
    ]
    assert len(approval) == 1
    assert approval[0].actor_id == ADMIN
    assert approval[0].checks is not None
    assert approval[0].checks[SELF_PUBLISHED_CHECK] is True


@pytest.mark.asyncio
async def test_ordinary_users_cannot_use_the_policy(tmp_path: Path) -> None:
    service = _service(tmp_path, knowledge_admin.LOCAL_SELF_PUBLISHERS)
    own = await service.submit_candidate(
        _as("exec-both"), draft(), idempotency_key=key()
    )
    for review in (
        service.approve(_as("exec-both"), own.ref, rationale="mine", checks=OK),
        service.reject(_as("exec-both"), own.ref, rationale="mine"),
    ):
        with pytest.raises(KnowledgeError) as error:
            await review
        assert error.value.code is KnowledgeErrorCode.SELF_REVIEW
    # A spoofed principal ID is still resolved server-side: an unknown
    # executive has no authority at all.
    with pytest.raises(AccessDenied):
        await service.approve(
            _as("exec-local-admin-2"), own.ref, rationale="x", checks=OK
        )


@pytest.mark.asyncio
async def test_independent_review_is_not_marked_self_published(tmp_path: Path) -> None:
    service = _service(tmp_path, knowledge_admin.LOCAL_SELF_PUBLISHERS)
    version = await service.submit_candidate(_as(ADMIN), draft(), idempotency_key=key())
    await service.approve(
        _as("exec-reviewer"), version.ref, rationale="Second person.", checks=OK
    )
    events = await service.history(_as("exec-reviewer"), version.ref)
    approval = next(e for e in events if e.action is ReviewAction.APPROVE)
    assert SELF_PUBLISHED_CHECK not in (approval.checks or {})


@pytest.mark.asyncio
async def test_default_wiring_keeps_independent_review_for_the_admin(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path, frozenset())
    version = await service.submit_candidate(_as(ADMIN), draft(), idempotency_key=key())
    with pytest.raises(KnowledgeError) as error:
        await service.approve(_as(ADMIN), version.ref, rationale="mine", checks=OK)
    assert error.value.code is KnowledgeErrorCode.SELF_REVIEW


@pytest.mark.asyncio
async def test_policy_keeps_permission_and_check_requirements(tmp_path: Path) -> None:
    service = _service(tmp_path, knowledge_admin.LOCAL_SELF_PUBLISHERS)
    version = await service.submit_candidate(_as(ADMIN), draft(), idempotency_key=key())
    narrowed = Principal(ADMIN, frozenset({Permission.ANALYSIS_READ.value}))
    with pytest.raises(AccessDenied):
        await service.approve(narrowed, version.ref, rationale="x", checks=OK)
    incomplete = OK.__class__(correct=True, sanitized=False, applicable=True)
    with pytest.raises(KnowledgeError) as error:
        await service.approve(_as(ADMIN), version.ref, rationale="x", checks=incomplete)
    assert error.value.code is KnowledgeErrorCode.INVALID_REQUEST


# -- the example file -------------------------------------------------------


def test_example_file_parses_into_a_shared_authored_draft() -> None:
    parsed = knowledge_admin.draft_from_json(
        '{"question": "q", "sql": "SELECT 1", "method_summary": "m",'
        ' "report_markdown": "# r", "metrics": [{"metric_id": "revenue",'
        ' "version": 1}], "sanitization_attested": true}'
    )
    assert parsed.access.is_shared
    assert parsed.sanitization_attested
    assert {m.metric_id for m in parsed.applicability.metrics} == {"revenue"}


def test_example_file_errors_name_the_problem() -> None:
    with pytest.raises(click.UsageError, match="missing field 'sql'"):
        knowledge_admin.draft_from_json(
            '{"question": "q", "method_summary": "m", "report_markdown": "r"}'
        )
