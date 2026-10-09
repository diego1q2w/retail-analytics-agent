"""Access-change auditing against real PostgreSQL (``pytest -m docker``)."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator

import psycopg
import pytest

from retail_analytics.application.authorization import AccessDenied
from retail_analytics.application.contracts.authorization import (
    ExecutiveRegistration,
    Principal,
)
from retail_analytics.bootstrap.access import build_access, build_access_audit
from retail_analytics.bootstrap.dev_access import (
    DEMO_EXECUTIVES,
    provision_demo_executives,
)
from retail_analytics.bootstrap.persistence import Persistence, build_persistence
from retail_analytics.domain.access import Permission, Role
from tests.integration.compose_stack import Stack, running_stack
from tests.integration.test_access import ISSUER, _authority

pytestmark = [pytest.mark.docker, pytest.mark.asyncio]


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


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _registration(
    executive_id: str, roles: frozenset[Role], label: str = "Test executive"
) -> ExecutiveRegistration:
    return ExecutiveRegistration(
        executive_id=executive_id,
        issuer=ISSUER,
        subject=f"sub-{executive_id}",
        roles=roles,
        label=label,
    )


def _rows(stack: Stack, executive_id: str) -> list[tuple[str, str, dict[str, object]]]:
    with psycopg.connect(stack.app_dsn) as conn:
        found = conn.execute(
            "SELECT action, actor_id, details FROM audit_events "
            "WHERE subject_type = 'executive' AND subject_id = %s "
            "ORDER BY occurred_at, audit_id",
            (executive_id,),
        ).fetchall()
    return [(a, b, dict(c)) for a, b, c in found]


async def test_each_mutation_kind_writes_exactly_one_audit_row(
    stack: Stack, db: Persistence
) -> None:
    admin = db.access_admin
    ex = _id("exec")
    await admin.register_executive(
        _registration(ex, frozenset({Role.EXECUTIVE})), actor_id="exec-boss"
    )
    await admin.register_executive(
        _registration(ex, frozenset({Role.EXECUTIVE, Role.EDITOR})),
        actor_id="exec-boss",
    )
    await admin.register_executive(
        _registration(ex, frozenset({Role.EXECUTIVE, Role.EDITOR}), "Renamed"),
        actor_id="exec-boss",
    )
    await admin.replace_products(ex, {"1", "2", "3"}, actor_id="exec-boss")
    await admin.replace_products(ex, {"2", "3", "4", "5"})
    await admin.set_active(ex, False, actor_id="exec-boss")
    await admin.set_active(ex, True, actor_id="exec-boss")

    rows = _rows(stack, ex)
    assert [r[0] for r in rows] == [
        "access.executive_registered",
        "access.roles_changed",
        "access.profile_changed",
        "access.entitlements_changed",
        "access.entitlements_changed",
        "access.deactivated",
        "access.activated",
    ]
    assert [r[1] for r in rows] == ["exec-boss"] * 4 + ["system:operator"] + [
        "exec-boss"
    ] * 2
    versions = [
        (r[2]["old_authorization_version"], r[2]["new_authorization_version"])
        for r in rows
    ]
    assert versions == [(None, 1), (1, 2), (2, 3), (3, 4), (4, 5), (5, 6), (6, 7)]
    assert rows[1][2]["roles_granted"] == ["editor"]
    assert rows[1][2]["roles_revoked"] == []
    assert rows[2][2]["label_changed"] is True
    second = rows[4][2]
    assert (second["products_added"], second["products_removed"]) == (2, 1)
    assert (second["products_before"], second["products_after"]) == (3, 4)
    access = await db.executives.get(ex)
    assert access is not None and access.authorization_version == 7


async def test_no_op_updates_write_nothing_and_secrets_are_absent(
    stack: Stack, db: Persistence
) -> None:
    admin = db.access_admin
    ex = _id("exec")
    registration = _registration(ex, frozenset({Role.EXECUTIVE}), "Private label")
    await admin.register_executive(registration)
    await admin.replace_products(ex, {"11", "12"})
    await admin.set_active(ex, True)
    before = _rows(stack, ex)
    await admin.register_executive(registration)
    await admin.replace_products(ex, {"12", "11"})
    await admin.set_active(ex, True)
    assert _rows(stack, ex) == before
    assert len(before) == 2

    blob = json.dumps(before)
    for forbidden in ("Private label", f"sub-{ex}", "'11'", '"11"', '"12"'):
        assert forbidden not in blob


async def test_audit_failure_rolls_back_every_mutation_kind(
    stack: Stack, db: Persistence
) -> None:
    admin = db.access_admin
    ex = _id("exec")
    await admin.register_executive(_registration(ex, frozenset({Role.EXECUTIVE})))
    await admin.replace_products(ex, {"1"})
    baseline = await db.executives.get(ex)
    rows_before = _rows(stack, ex)

    with psycopg.connect(stack.app_dsn) as conn:
        conn.execute(
            "CREATE FUNCTION fail_access_audit() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN "
            "IF NEW.action LIKE 'access.%' THEN "
            "RAISE EXCEPTION 'injected audit failure'; END IF; RETURN NEW; END; $$"
        )
        conn.execute(
            "CREATE TRIGGER fail_access_audit BEFORE INSERT ON audit_events "
            "FOR EACH ROW EXECUTE FUNCTION fail_access_audit()"
        )
        conn.commit()
    try:
        with pytest.raises(Exception, match="injected audit failure"):
            await admin.register_executive(
                _registration(ex, frozenset({Role.EXECUTIVE, Role.ADMIN}))
            )
        with pytest.raises(Exception, match="injected audit failure"):
            await admin.replace_products(ex, {"2", "3"})
        with pytest.raises(Exception, match="injected audit failure"):
            await admin.set_active(ex, False)
        with pytest.raises(Exception, match="injected audit failure"):
            await admin.register_executive(
                _registration(_id("exec"), frozenset({Role.EXECUTIVE}))
            )
        assert await db.executives.get(ex) == baseline
        assert _rows(stack, ex) == rows_before
    finally:
        with psycopg.connect(stack.app_dsn) as conn:
            conn.execute("DROP TRIGGER fail_access_audit ON audit_events")
            conn.execute("DROP FUNCTION fail_access_audit()")
            conn.commit()

    await admin.set_active(ex, False)  # works again once the fault is gone


async def test_access_audit_rows_are_immutable(stack: Stack, db: Persistence) -> None:
    ex = _id("exec")
    await db.access_admin.register_executive(
        _registration(ex, frozenset({Role.EXECUTIVE}))
    )
    with psycopg.connect(stack.app_dsn) as conn, pytest.raises(psycopg.Error):
        conn.execute(
            "UPDATE audit_events SET actor_id = 'x' WHERE subject_id = %s", (ex,)
        )


async def test_history_is_admin_only_and_newest_first(
    stack: Stack, db: Persistence
) -> None:
    admin_id, target, plain = _id("admin"), _id("exec"), _id("plain")
    await db.access_admin.register_executive(
        _registration(admin_id, frozenset({Role.ADMIN}))
    )
    await db.access_admin.register_executive(
        _registration(plain, frozenset({Role.EXECUTIVE}))
    )
    await db.access_admin.register_executive(
        _registration(target, frozenset({Role.EXECUTIVE})), actor_id=admin_id
    )
    await db.access_admin.replace_products(target, {"7"}, actor_id=admin_id)
    await db.access_admin.set_active(target, False, actor_id=admin_id)

    resolver = build_access(db, _authority()).resolver
    service = build_access_audit(db, resolver)
    admin = Principal(admin_id, frozenset({Permission.ACCESS_ADMIN}))
    history = await service.history(admin, target)
    assert [c.action for c in history] == [
        "access.deactivated",
        "access.entitlements_changed",
        "access.executive_registered",
    ]
    assert {c.actor_id for c in history} == {admin_id}
    assert (await service.history(admin, target, limit=1))[0].action == (
        "access.deactivated"
    )
    assert await service.history(admin, _id("none")) == []

    with pytest.raises(AccessDenied):
        await service.history(
            Principal(plain, frozenset({Permission.ACCESS_ADMIN})), target
        )
    with pytest.raises(AccessDenied):  # token scope narrows the admin role
        await service.history(Principal(admin_id, frozenset()), target)
    with pytest.raises(ValueError):
        await service.history(admin, target, limit=0)


async def test_dev_provisioning_is_audited_and_rerun_is_silent(
    stack: Stack, db: Persistence
) -> None:
    await provision_demo_executives(db.access_admin, ISSUER)
    first = {d.executive_id: _rows(stack, d.executive_id) for d in DEMO_EXECUTIVES}
    for demo in DEMO_EXECUTIVES:
        rows = first[demo.executive_id]
        assert [r[0] for r in rows][:2] == [
            "access.executive_registered",
            "access.entitlements_changed",
        ]
        assert {r[1] for r in rows} == {"system:dev-access"}
    await provision_demo_executives(db.access_admin, ISSUER)
    assert {d.executive_id: _rows(stack, d.executive_id) for d in DEMO_EXECUTIVES} == (
        first
    )
