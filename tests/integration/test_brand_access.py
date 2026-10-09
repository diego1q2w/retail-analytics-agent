"""Brand-based manager access against real PostgreSQL (T05-F2).

Assigned brands resolve, in the directory read, to the products whose synced
catalog brand matches exactly; the resolved set is the ``ProductScope`` every
existing check enforces. These tests drive that scope through the real
resolver into the compiler and result boundary (DuckDB oracle), the schema
context cache, evidence authority stamps and report required-scope coverage.

Needs Docker (``pytest -m docker``); runs against a throwaway Compose project.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import psycopg
import pytest

from retail_analytics.adapters.auth.local_jwt import LocalJwtAuthority
from retail_analytics.adapters.postgres.database import Database
from retail_analytics.adapters.postgres.product_scopes import (
    PostgresProductScopeSnapshots,
    record_snapshot,
)
from retail_analytics.application.authorization import AccessResolver
from retail_analytics.application.brand_access import (
    BrandAccessError,
    BrandAccessService,
)
from retail_analytics.application.contracts import access_audit
from retail_analytics.application.contracts.authorization import (
    ExecutiveRegistration,
    Principal,
)
from retail_analytics.application.contracts.brand_access import ProductBrandCatalog
from retail_analytics.application.contracts.persistence import RunRequest
from retail_analytics.application.contracts.tools import ExecutionContext
from retail_analytics.application.discovery import DiscoveryService, SourceSchemaCache
from retail_analytics.application.reports import ReportAccessRule
from retail_analytics.application.schema_context import ApprovedSchemaContext
from retail_analytics.bootstrap.access import build_access, build_brand_access
from retail_analytics.bootstrap.dev_access import (
    DEMO_EXECUTIVES,
    LOCAL_ADMIN,
    LOCAL_EXECUTIVES,
    provision_demo_executives,
)
from retail_analytics.bootstrap.persistence import Persistence, build_persistence
from retail_analytics.domain.access import Permission, ProductScope, Role
from retail_analytics.domain.evidence import AuthorityStamp
from tests.integration.compose_stack import Stack, running_stack
from tests.unit.discovery_fixtures import CATALOG, FakeClock, StubMetadata
from tests.unit.privacy.support import released
from tests.unit.sql_compiler.support import RAW, database

pytestmark = [pytest.mark.docker, pytest.mark.asyncio]
ISSUER = "retail-analytics-local"
KEY = "integration-test-signing-key-" + "b" * 40
ALL = frozenset(p.value for p in Permission)

# Brand per product in the compiler's DuckDB fixture: Alpha = 1, 3; Beta = 2.
BY_BRAND = (
    "SELECT p.brand, SUM(s.sale_amount) AS total FROM sales_items s "
    "JOIN products p ON s.product_id = p.product_id GROUP BY p.brand"
)


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


@pytest.fixture
def resolver(db: Persistence) -> AccessResolver:
    authority = LocalJwtAuthority(KEY, issuer=ISSUER, audience="retail-analytics-api")
    return build_access(db, authority).resolver


class _Catalog:
    """A ``ProductBrandSource`` whose snapshot a test can change."""

    def __init__(self, brands: dict[str, str]) -> None:
        self.brands = dict(brands)
        self.skipped = 0

    async def read_product_brands(self) -> ProductBrandCatalog:
        return ProductBrandCatalog(
            dict(self.brands), "test-catalog", products_without_brand=self.skipped
        )


def _duckdb_catalog() -> dict[str, str]:
    """``products.brand`` read from the same data the queries run on."""
    sql = f"SELECT id, brand FROM {RAW}products"  # noqa: S608
    rows = database().execute(sql).fetchall()
    return {str(i): str(b) for i, b in rows}


def _service(db: Persistence, catalog: _Catalog) -> BrandAccessService:
    return build_brand_access(db, catalog)


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


async def _manager(
    db: Persistence, roles: frozenset[Role] = frozenset({Role.EXECUTIVE})
) -> str:
    executive_id = _id("exec")
    await db.access_admin.register_executive(
        ExecutiveRegistration(
            executive_id=executive_id,
            issuer=ISSUER,
            subject=f"sub-{executive_id}",
            roles=roles,
            label="Brand manager",
        )
    )
    return executive_id


async def _context(
    db: Persistence, resolver: AccessResolver, executive_id: str
) -> ExecutionContext:
    session = await db.sessions.create_session(_id("ses"), executive_id)
    started = await db.runs.start_run(
        RunRequest(
            run_id=_id("run"),
            session_id=session.session_id,
            requested_by=executive_id,
            submission_key=_id("key"),
            message_id=_id("msg"),
            request_text="Revenue by brand",
        )
    )
    principal = Principal(executive_id, ALL)
    return await resolver.context_for_run(principal, started.run.run_id)


async def _scope(db: Persistence, executive_id: str) -> ProductScope:
    access = await db.executives.get(executive_id)
    assert access is not None
    return access.product_scope


def _audit(stack: Stack, executive_id: str) -> list[tuple[str, dict[str, object]]]:
    with psycopg.connect(stack.app_dsn) as conn:
        rows = conn.execute(
            "SELECT action, details FROM audit_events WHERE subject_id = %s "
            "ORDER BY occurred_at, audit_id",
            (executive_id,),
        ).fetchall()
    return [(r[0], r[1]) for r in rows]


def _brands_in(result_rows: tuple[tuple[object, ...], ...]) -> set[object]:
    return {row[0] for row in result_rows}


async def test_two_brand_managers_see_only_their_brands_everywhere(
    db: Persistence, resolver: AccessResolver
) -> None:
    catalog = _Catalog(_duckdb_catalog())
    brands = _service(db, catalog)
    await brands.sync_catalog()
    alpha, beta = await _manager(db), await _manager(db)
    await brands.assign(alpha, ["Alpha"])
    await brands.assign(beta, ["Beta"])

    ctx_a = await _context(db, resolver, alpha)
    ctx_b = await _context(db, resolver, beta)
    assert ctx_a.product_scope.product_ids == {"1", "3"}
    assert ctx_b.product_scope.product_ids == {"2"}

    # Data: the same query, compiled and released per manager, returns only
    # the manager's own brand (the compiler binds the resolved product set).
    data = database()
    seen_a = released(data, alpha, BY_BRAND, ctx_a.product_scope)
    seen_b = released(data, beta, BY_BRAND, ctx_b.product_scope)
    assert _brands_in(seen_a.rows) == {"Alpha"}
    assert _brands_in(seen_b.rows) == {"Beta"}

    # Schema context: separate cache entries keyed by executive and products.
    clock = FakeClock()
    discovery = DiscoveryService(
        CATALOG, SourceSchemaCache(CATALOG, StubMetadata(), clock), clock
    )
    schema = ApprovedSchemaContext(discovery)
    first_a = await schema.for_context(ctx_a)
    first_b = await schema.for_context(ctx_b)
    assert schema.size == 2 and first_a is not first_b

    # Evidence and reports: A's authority stamp and A's saved scope are never
    # valid for B.
    stamp_a = AuthorityStamp.of(ctx_a.product_scope)
    assert stamp_a.matches(ctx_a.product_scope)
    assert not stamp_a.matches(ctx_b.product_scope)
    pg = Database(db.engine)
    saved_a = await pg.transaction(
        lambda c: record_snapshot(c, ctx_a.product_scope.product_ids, pg.clock())
    )
    rule = ReportAccessRule(PostgresProductScopeSnapshots(pg))
    assert await rule.readable(ctx_a.product_scope, [(saved_a, "x")]) == (True,)
    assert await rule.readable(ctx_b.product_scope, [(saved_a, "x")]) == (False,)


async def test_removing_a_brand_narrows_access_through_existing_checks(
    stack: Stack, db: Persistence, resolver: AccessResolver
) -> None:
    catalog = _Catalog(_duckdb_catalog())
    brands = _service(db, catalog)
    await brands.sync_catalog()
    manager = await _manager(db)
    both = await brands.assign(manager, ["Alpha", "Beta"])
    assert both.brand_products == {"Alpha": 2, "Beta": 1}
    before = await _context(db, resolver, manager)
    assert before.product_scope.product_ids == {"1", "2", "3"}
    stamp = AuthorityStamp.of(before.product_scope)
    pg = Database(db.engine)
    saved = await pg.transaction(
        lambda c: record_snapshot(c, before.product_scope.product_ids, pg.clock())
    )

    narrowed = await brands.remove(manager, ["Beta"])
    assert narrowed.brands == {"Alpha"}
    after = await _context(db, resolver, manager)
    assert after.product_scope.product_ids == {"1", "3"}
    assert (
        after.product_scope.entitlement_version
        == before.product_scope.entitlement_version + 1
    )
    # Cached evidence no longer matches, the saved report is access_changed,
    # and the next query cannot return Beta.
    assert not stamp.matches(after.product_scope)
    rule = ReportAccessRule(PostgresProductScopeSnapshots(pg))
    assert await rule.readable(after.product_scope, [(saved, "x")]) == (False,)
    seen = released(database(), manager, BY_BRAND, after.product_scope)
    assert _brands_in(seen.rows) == {"Alpha"}

    actions = _audit(stack, manager)
    changes = [d for a, d in actions if a == access_audit.BRANDS_CHANGED]
    assert [c["brands_assigned"] for c in changes] == [["Alpha", "Beta"], []]
    assert [c["brands_removed"] for c in changes] == [[], ["Beta"]]
    assert changes[1]["products_removed"] == 1
    # Counts and digests only, never product lists.
    assert not any("product_ids" in key for key in changes[1])

    # Removing a brand that is not assigned changes and records nothing.
    again = await brands.remove(manager, ["Beta"])
    assert again.access.authorization_version == (
        after.product_scope.entitlement_version
    )


async def test_catalog_brand_change_moves_products_between_managers(
    stack: Stack, db: Persistence
) -> None:
    catalog = _Catalog({"1": "Alpha", "2": "Beta", "3": "Alpha"})
    brands = _service(db, catalog)
    await brands.sync_catalog()
    alpha, beta = await _manager(db), await _manager(db)
    await brands.assign(alpha, ["Alpha"])
    await brands.assign(beta, ["Beta"])
    bystander = await _manager(db)
    await db.access_admin.replace_products(bystander, {"3"})
    old = {e: await _scope(db, e) for e in (alpha, beta, bystander)}

    # Product 3 moves from Alpha to Beta in the trusted catalog.
    catalog.brands["3"] = "Beta"
    result = await brands.sync_catalog()
    assert result.products_changed == 1
    changed = {c.executive_id: c for c in result.executives_changed}
    assert set(changed) >= {alpha, beta} and bystander not in changed
    new = {e: await _scope(db, e) for e in (alpha, beta, bystander)}
    assert new[alpha].product_ids == {"1"}
    assert new[beta].product_ids == {"2", "3"}
    for executive_id in (alpha, beta):
        assert (
            new[executive_id].entitlement_version
            == old[executive_id].entitlement_version + 1
        )
        assert not AuthorityStamp.of(old[executive_id]).matches(new[executive_id])
    # Explicit grants are unaffected by the catalog.
    assert new[bystander] == old[bystander]
    moved = [d for a, d in _audit(stack, alpha) if a == "access.brand_catalog_changed"]
    assert moved[-1]["products_removed"] == 1

    # Re-syncing the same catalog changes nothing.
    again = await brands.sync_catalog()
    assert again.products_changed == 0 and again.executives_changed == ()
    with psycopg.connect(stack.app_dsn) as conn:
        synced = conn.execute(
            "SELECT count(*) FROM audit_events WHERE action = %s AND subject_id = %s",
            (access_audit.BRAND_CATALOG_SYNCED, result.catalog_digest),
        ).fetchone()
    assert synced is not None and synced[0] == 1


async def test_unknown_and_inexact_brands_grant_nothing(db: Persistence) -> None:
    catalog = _Catalog({"1": "Alpha", "2": "Beta", "3": "Alpha"})
    brands = _service(db, catalog)
    await brands.sync_catalog()
    manager = await _manager(db)

    for wrong in (["alpha"], ["ALPHA"], ["Alph"], ["Gamma"]):
        with pytest.raises(BrandAccessError) as refused:
            await brands.assign(manager, wrong)
        assert refused.value.code == "unknown_brand"
    with pytest.raises(BrandAccessError) as invalid:
        await brands.assign(manager, [" Alpha"])
    assert invalid.value.code == "invalid_brand"
    assert (await _scope(db, manager)).is_empty

    # Stored explicitly, an unmatched brand still grants nothing...
    stored = await brands.assign(manager, ["Gamma"], allow_unmatched=True)
    assert stored.unmatched == {"Gamma"}
    assert (await _scope(db, manager)).is_empty
    # ...until the trusted catalog contains it (documented behaviour).
    catalog.brands["4"] = "Gamma"
    await brands.sync_catalog()
    assert (await _scope(db, manager)).product_ids == {"4"}

    # Products without a usable brand are never stored, so never granted.
    catalog.brands.pop("4")
    catalog.skipped = 2
    synced = await brands.sync_catalog()
    assert synced.products_without_brand == 2
    assert (await _scope(db, manager)).is_empty

    # An empty catalog is refused and the previous snapshot stays in force.
    await brands.assign(manager, ["Alpha"])
    empty = _Catalog({})
    with pytest.raises(BrandAccessError) as refused_empty:
        await _service(db, empty).sync_catalog()
    assert refused_empty.value.code == "empty_catalog"
    assert (await _scope(db, manager)).product_ids == {"1", "3"}


async def test_admin_role_alone_grants_no_products(
    db: Persistence, resolver: AccessResolver
) -> None:
    catalog = _Catalog({"1": "Alpha", "2": "Beta", "3": "Alpha"})
    await _service(db, catalog).sync_catalog()
    admin = await _manager(db, frozenset({Role.ADMIN, Role.EXECUTIVE}))
    access = await db.executives.get(admin)
    assert access is not None
    assert Permission.ACCESS_ADMIN in access.permissions
    assert access.product_scope.is_empty
    seen = (await _context(db, resolver, admin)).product_scope
    assert seen.is_empty


async def test_explicit_grants_and_brands_combine_and_deactivation_wins(
    db: Persistence,
) -> None:
    catalog = _Catalog({"1": "Alpha", "2": "Beta", "3": "Alpha"})
    brands = _service(db, catalog)
    await brands.sync_catalog()
    manager = await _manager(db)
    await db.access_admin.replace_products(manager, {"1"})
    await brands.assign(manager, ["Alpha"])
    assert (await _scope(db, manager)).product_ids == {"1", "3"}
    # Removing the brand keeps the explicit grant of an overlapping product.
    await brands.remove(manager, ["Alpha"])
    assert (await _scope(db, manager)).product_ids == {"1"}
    await brands.assign(manager, ["Beta"])
    await db.access_admin.set_active(manager, False)
    assert (await _scope(db, manager)).is_empty


async def test_demo_provisioning_assigns_brands_and_keeps_the_admin_grant(
    db: Persistence,
) -> None:
    first = await provision_demo_executives(
        db.access_admin, ISSUER, LOCAL_EXECUTIVES, brands=db.brand_access
    )
    again = await provision_demo_executives(
        db.access_admin, ISSUER, LOCAL_EXECUTIVES, brands=db.brand_access
    )
    assert first == again  # a rerun changes nothing, not even the version
    admin = first[0]
    assert admin.executive_id == LOCAL_ADMIN.executive_id
    assert admin.product_ids >= LOCAL_ADMIN.product_ids
    for demo in DEMO_EXECUTIVES:
        assert await db.brand_access.brands_of(demo.executive_id) == demo.brands
