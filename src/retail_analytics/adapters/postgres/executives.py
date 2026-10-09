"""PostgreSQL ``ExecutiveDirectory``, ``AccessAdministration`` and
``BrandAccessStore``.

Reads take the executive row and its product set in one statement, so the
version and the products always belong to the same committed state. The
product set is the explicit grants plus the products whose snapshot brand is
assigned to the executive (exact match). Writes lock the executive row, apply
the change, increment ``authorization_version`` and append one ``access.*``
audit event in one transaction (an audit failure rolls the change back);
identical repeats change and record nothing. Brand changes and catalog syncs
also take one transaction-level advisory lock, so a sync and an assignment
never interleave (each sees the other's committed result and versions it).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping

import sqlalchemy as sa
from sqlalchemy import exc
from sqlalchemy.dialects.postgresql import insert

from retail_analytics.adapters.postgres.audit import append_audit
from retail_analytics.adapters.postgres.database import Database, violated_constraint
from retail_analytics.adapters.postgres.schema import (
    catalog_product_brands,
    executive_brands,
    executives,
    product_entitlements,
)
from retail_analytics.application.contracts import access_audit
from retail_analytics.application.contracts.access_audit import SYSTEM_ACTOR
from retail_analytics.application.contracts.audit import AuditEvent
from retail_analytics.application.contracts.authorization import ExecutiveRegistration
from retail_analytics.application.contracts.brand_access import (
    BrandCatalogSync,
    ExecutiveScopeChange,
    ProductBrandCatalog,
)
from retail_analytics.application.contracts.persistence import (
    IdempotencyConflict,
    RecordNotFound,
)
from retail_analytics.domain.access import (
    ExecutiveAccess,
    Role,
    is_valid_brand,
    is_valid_product_id,
)

_PRODUCTS = (
    sa.select(sa.func.array_agg(product_entitlements.c.product_id))
    .where(product_entitlements.c.executive_id == executives.c.executive_id)
    .scalar_subquery()
    .label("product_ids")
)

# Products of the executive's assigned brands in the synced catalog snapshot.
_BRAND_PRODUCTS = (
    sa.select(sa.func.array_agg(catalog_product_brands.c.product_id))
    .select_from(
        catalog_product_brands.join(
            executive_brands,
            executive_brands.c.brand == catalog_product_brands.c.brand,
        )
    )
    .where(executive_brands.c.executive_id == executives.c.executive_id)
    .scalar_subquery()
    .label("brand_product_ids")
)

# Serializes brand assignments and catalog syncs (any constant works; this one
# is "T05F2" in ASCII).
_BRAND_LOCK_KEY = 0x5430354632


def _select() -> sa.Select[tuple[object, ...]]:
    return sa.select(
        executives.c.executive_id,
        executives.c.roles,
        executives.c.active,
        executives.c.authorization_version,
        _PRODUCTS,
        _BRAND_PRODUCTS,
    )


def _access(row: sa.Row[tuple[object, ...]]) -> ExecutiveAccess:
    m = row._mapping
    return ExecutiveAccess(
        executive_id=m["executive_id"],
        roles=frozenset(Role(role) for role in m["roles"]),
        product_ids=frozenset(m["product_ids"] or ())
        | frozenset(m["brand_product_ids"] or ()),
        active=m["active"],
        authorization_version=m["authorization_version"],
    )


def _load(connection: sa.Connection, executive_id: str) -> ExecutiveAccess:
    row = connection.execute(
        _select().where(executives.c.executive_id == executive_id)
    ).one_or_none()
    if row is None:
        raise RecordNotFound("executive", executive_id)
    return _access(row)


def _lock(connection: sa.Connection, executive_id: str) -> sa.Row[tuple[object, ...]]:
    row = connection.execute(
        sa.select(executives)
        .where(executives.c.executive_id == executive_id)
        .with_for_update()
    ).one_or_none()
    if row is None:
        raise RecordNotFound("executive", executive_id)
    return row


def _brand_lock(connection: sa.Connection) -> None:
    connection.execute(sa.select(sa.func.pg_advisory_xact_lock(_BRAND_LOCK_KEY)))


def _scope_details(before: frozenset[str], after: frozenset[str]) -> dict[str, object]:
    return {
        "products_added": len(after - before),
        "products_removed": len(before - after),
        "added_digest": _digest(after - before),
        "removed_digest": _digest(before - after),
        "products_before": len(before),
        "products_after": len(after),
        "products_after_digest": _digest(after),
    }


def _digest(product_ids: Iterable[str]) -> str:
    """Stable fingerprint of a product set; the IDs themselves are not stored."""
    joined = "\n".join(sorted(product_ids))
    return hashlib.sha256(joined.encode()).hexdigest()[:16]


class PostgresExecutiveDirectory:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def find_by_subject(
        self, issuer: str, subject: str
    ) -> ExecutiveAccess | None:
        return await self._db.transaction(self._find, issuer, subject)

    @staticmethod
    def _find(
        connection: sa.Connection, issuer: str, subject: str
    ) -> ExecutiveAccess | None:
        row = connection.execute(
            _select().where(
                executives.c.issuer == issuer, executives.c.subject == subject
            )
        ).one_or_none()
        return None if row is None else _access(row)

    async def get(self, executive_id: str) -> ExecutiveAccess | None:
        return await self._db.transaction(self._get, executive_id)

    @staticmethod
    def _get(connection: sa.Connection, executive_id: str) -> ExecutiveAccess | None:
        row = connection.execute(
            _select().where(executives.c.executive_id == executive_id)
        ).one_or_none()
        return None if row is None else _access(row)

    async def register_executive(
        self, registration: ExecutiveRegistration, *, actor_id: str = SYSTEM_ACTOR
    ) -> ExecutiveAccess:
        try:
            return await self._db.transaction(self._register, registration, actor_id)
        except exc.IntegrityError as error:
            if violated_constraint(error) == "uq_executives_identity":
                raise IdempotencyConflict(
                    "executive identity", registration.executive_id
                ) from None
            raise

    def _register(
        self,
        connection: sa.Connection,
        registration: ExecutiveRegistration,
        actor_id: str,
    ) -> ExecutiveAccess:
        now = self._db.clock()
        roles = sorted(role.value for role in registration.roles)
        created = connection.execute(
            insert(executives)
            .values(
                executive_id=registration.executive_id,
                issuer=registration.issuer,
                subject=registration.subject,
                label=registration.label,
                roles=roles,
                active=True,
                authorization_version=1,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=["executive_id"])
            .returning(executives.c.executive_id)
        ).one_or_none()
        current = _lock(connection, registration.executive_id)._mapping
        if (current["issuer"], current["subject"]) != (
            registration.issuer,
            registration.subject,
        ):
            raise IdempotencyConflict("executive", registration.executive_id)
        if created is not None:
            self._audit(
                connection,
                actor_id,
                registration.executive_id,
                access_audit.REGISTERED,
                old_version=None,
                new_version=1,
                roles=roles,
            )
        elif (sorted(current["roles"]), current["label"]) != (
            roles,
            registration.label,
        ):
            old_roles = set(current["roles"])
            label_changed = current["label"] != registration.label
            self._bump(
                connection,
                actor_id,
                registration.executive_id,
                current["authorization_version"],
                access_audit.ROLES_CHANGED
                if old_roles != set(roles)
                else access_audit.PROFILE_CHANGED,
                {
                    "roles_granted": sorted(set(roles) - old_roles),
                    "roles_revoked": sorted(old_roles - set(roles)),
                    "label_changed": label_changed,
                },
                roles=roles,
                label=registration.label,
            )
        return _load(connection, registration.executive_id)

    async def replace_products(
        self,
        executive_id: str,
        product_ids: Iterable[str],
        *,
        actor_id: str = SYSTEM_ACTOR,
    ) -> ExecutiveAccess:
        wanted = frozenset(product_ids)
        invalid = [p for p in wanted if not is_valid_product_id(p)]
        if invalid:
            raise ValueError(f"{len(invalid)} invalid product IDs")
        return await self._db.transaction(self._replace, executive_id, wanted, actor_id)

    def _replace(
        self,
        connection: sa.Connection,
        executive_id: str,
        wanted: frozenset[str],
        actor_id: str,
    ) -> ExecutiveAccess:
        version = _lock(connection, executive_id)._mapping["authorization_version"]
        current = frozenset(
            connection.execute(
                sa.select(product_entitlements.c.product_id).where(
                    product_entitlements.c.executive_id == executive_id
                )
            ).scalars()
        )
        removed, added = current - wanted, wanted - current
        if removed:
            connection.execute(
                sa.delete(product_entitlements).where(
                    product_entitlements.c.executive_id == executive_id,
                    product_entitlements.c.product_id.in_(sorted(removed)),
                )
            )
        if added:
            now = self._db.clock()
            connection.execute(
                sa.insert(product_entitlements),
                [
                    {"executive_id": executive_id, "product_id": p, "granted_at": now}
                    for p in sorted(added)
                ],
            )
        if removed or added:
            self._bump(
                connection,
                actor_id,
                executive_id,
                version,
                access_audit.ENTITLEMENTS_CHANGED,
                {
                    "products_added": len(added),
                    "products_removed": len(removed),
                    "added_digest": _digest(added),
                    "removed_digest": _digest(removed),
                    "products_before": len(current),
                    "products_after": len(wanted),
                    "products_after_digest": _digest(wanted),
                },
            )
        return _load(connection, executive_id)

    async def set_active(
        self, executive_id: str, active: bool, *, actor_id: str = SYSTEM_ACTOR
    ) -> ExecutiveAccess:
        return await self._db.transaction(
            self._set_active, executive_id, active, actor_id
        )

    def _set_active(
        self,
        connection: sa.Connection,
        executive_id: str,
        active: bool,
        actor_id: str,
    ) -> ExecutiveAccess:
        current = _lock(connection, executive_id)._mapping
        if current["active"] != active:
            self._bump(
                connection,
                actor_id,
                executive_id,
                current["authorization_version"],
                access_audit.ACTIVATED if active else access_audit.DEACTIVATED,
                {},
                active=active,
            )
        return _load(connection, executive_id)

    # -- brands (BrandAccessStore) ------------------------------------------

    async def brands_of(self, executive_id: str) -> frozenset[str]:
        return await self._db.transaction(self._brands_of, executive_id)

    @staticmethod
    def _brands_of(connection: sa.Connection, executive_id: str) -> frozenset[str]:
        exists = connection.execute(
            sa.select(executives.c.executive_id).where(
                executives.c.executive_id == executive_id
            )
        ).one_or_none()
        if exists is None:
            raise RecordNotFound("executive", executive_id)
        return frozenset(
            connection.execute(
                sa.select(executive_brands.c.brand).where(
                    executive_brands.c.executive_id == executive_id
                )
            ).scalars()
        )

    async def catalog_brand_sizes(self, brands: Iterable[str]) -> Mapping[str, int]:
        wanted = sorted(set(brands))
        if not wanted:
            return {}
        return await self._db.transaction(self._brand_sizes, wanted)

    @staticmethod
    def _brand_sizes(connection: sa.Connection, brands: list[str]) -> dict[str, int]:
        rows = connection.execute(
            sa.select(catalog_product_brands.c.brand, sa.func.count())
            .where(catalog_product_brands.c.brand.in_(brands))
            .group_by(catalog_product_brands.c.brand)
        )
        found = {brand: count for brand, count in rows}
        return {brand: found.get(brand, 0) for brand in brands}

    async def brands_within(self, product_ids: Iterable[str]) -> frozenset[str]:
        wanted = sorted(set(product_ids))
        if not wanted:
            return frozenset()
        return await self._db.transaction(self._brands_within, wanted)

    @staticmethod
    def _brands_within(connection: sa.Connection, ids: list[str]) -> frozenset[str]:
        rows = connection.execute(
            sa.select(catalog_product_brands.c.brand)
            .where(
                catalog_product_brands.c.product_id
                == sa.func.any(sa.literal(ids, sa.ARRAY(sa.Text)))
            )
            .distinct()
        )
        return frozenset(brand for (brand,) in rows)

    async def replace_brands(
        self,
        executive_id: str,
        brands: Iterable[str],
        *,
        actor_id: str = SYSTEM_ACTOR,
    ) -> ExecutiveAccess:
        wanted = frozenset(brands)
        invalid = [b for b in wanted if not is_valid_brand(b)]
        if invalid:
            raise ValueError(f"{len(invalid)} invalid brand names")
        return await self._db.transaction(
            self._replace_brands, executive_id, wanted, actor_id
        )

    def _replace_brands(
        self,
        connection: sa.Connection,
        executive_id: str,
        wanted: frozenset[str],
        actor_id: str,
    ) -> ExecutiveAccess:
        _brand_lock(connection)
        version = _lock(connection, executive_id)._mapping["authorization_version"]
        current = self._brands_of(connection, executive_id)
        removed, added = current - wanted, wanted - current
        if not (removed or added):
            return _load(connection, executive_id)
        before = _load(connection, executive_id).product_ids
        if removed:
            connection.execute(
                sa.delete(executive_brands).where(
                    executive_brands.c.executive_id == executive_id,
                    executive_brands.c.brand.in_(sorted(removed)),
                )
            )
        if added:
            now = self._db.clock()
            connection.execute(
                sa.insert(executive_brands),
                [
                    {"executive_id": executive_id, "brand": b, "assigned_at": now}
                    for b in sorted(added)
                ],
            )
        after = _load(connection, executive_id).product_ids
        self._bump(
            connection,
            actor_id,
            executive_id,
            version,
            access_audit.BRANDS_CHANGED,
            {
                "brands_assigned": sorted(added),
                "brands_removed": sorted(removed),
                "brands_after": len(wanted),
                **_scope_details(before, after),
            },
        )
        return _load(connection, executive_id)

    async def sync_catalog(
        self, catalog: ProductBrandCatalog, *, actor_id: str = SYSTEM_ACTOR
    ) -> BrandCatalogSync:
        return await self._db.transaction(self._sync_catalog, catalog, actor_id)

    def _sync_catalog(
        self,
        connection: sa.Connection,
        catalog: ProductBrandCatalog,
        actor_id: str,
    ) -> BrandCatalogSync:
        _brand_lock(connection)
        branded = list(
            connection.execute(
                sa.select(executives.c.executive_id, executives.c.authorization_version)
                .where(
                    executives.c.executive_id.in_(
                        sa.select(executive_brands.c.executive_id).distinct()
                    )
                )
                .order_by(executives.c.executive_id)
                .with_for_update()
            )
        )
        before = {e: _load(connection, e).product_ids for e, _ in branded}
        stored = {
            row.product_id: row.brand
            for row in connection.execute(
                sa.select(
                    catalog_product_brands.c.product_id,
                    catalog_product_brands.c.brand,
                )
            )
        }
        wanted = dict(catalog.brands)
        stale = sorted(p for p, b in stored.items() if wanted.get(p) != b)
        fresh = sorted(p for p, b in wanted.items() if stored.get(p) != b)
        for start in range(0, len(stale), 5000):
            connection.execute(
                sa.delete(catalog_product_brands).where(
                    catalog_product_brands.c.product_id.in_(stale[start : start + 5000])
                )
            )
        if fresh:
            now = self._db.clock()
            connection.execute(
                sa.insert(catalog_product_brands),
                [
                    {"product_id": p, "brand": wanted[p], "synced_at": now}
                    for p in fresh
                ],
            )
        changed: list[ExecutiveScopeChange] = []
        for executive_id, version in branded:
            after = _load(connection, executive_id).product_ids
            if after == before[executive_id]:
                continue
            self._bump(
                connection,
                actor_id,
                executive_id,
                version,
                access_audit.BRAND_CATALOG_CHANGED,
                {
                    "catalog_digest": catalog.digest,
                    "catalog_source": catalog.source_ref,
                    **_scope_details(before[executive_id], after),
                },
            )
            changed.append(
                ExecutiveScopeChange(
                    executive_id=executive_id,
                    old_authorization_version=version,
                    new_authorization_version=version + 1,
                    products_before=len(before[executive_id]),
                    products_after=len(after),
                )
            )
        products_changed = len(set(stale) | set(fresh))
        result = BrandCatalogSync(
            catalog_digest=catalog.digest,
            source_ref=catalog.source_ref,
            products=len(wanted),
            brands=len(set(wanted.values())),
            products_without_brand=catalog.products_without_brand,
            products_changed=products_changed,
            executives_changed=tuple(changed),
        )
        if products_changed:
            append_audit(
                connection,
                AuditEvent(
                    audit_id=self._db.new_id(),
                    occurred_at=self._db.clock(),
                    actor_id=actor_id,
                    action=access_audit.BRAND_CATALOG_SYNCED,
                    subject_type=access_audit.CATALOG_SUBJECT_TYPE,
                    subject_id=catalog.digest,
                    details={
                        "actor_id": actor_id,
                        "catalog_source": catalog.source_ref,
                        "products": result.products,
                        "brands": result.brands,
                        "products_without_brand": result.products_without_brand,
                        "products_changed": products_changed,
                        "executives_changed": len(changed),
                    },
                ),
            )
        return result

    def _bump(
        self,
        connection: sa.Connection,
        actor_id: str,
        executive_id: str,
        old_version: int,
        action: str,
        details: dict[str, object],
        **changes: object,
    ) -> None:
        """Apply the change, bump the version and audit it, atomically."""
        connection.execute(
            sa.update(executives)
            .where(executives.c.executive_id == executive_id)
            .values(
                **changes,
                authorization_version=old_version + 1,
                updated_at=self._db.clock(),
            )
        )
        self._audit(
            connection,
            actor_id,
            executive_id,
            action,
            old_version=old_version,
            new_version=old_version + 1,
            **details,
        )

    def _audit(
        self,
        connection: sa.Connection,
        actor_id: str,
        executive_id: str,
        action: str,
        *,
        old_version: int | None,
        new_version: int,
        **details: object,
    ) -> None:
        append_audit(
            connection,
            AuditEvent(
                audit_id=self._db.new_id(),
                occurred_at=self._db.clock(),
                actor_id=actor_id,
                action=action,
                subject_type=access_audit.SUBJECT_TYPE,
                subject_id=executive_id,
                details={
                    "actor_id": actor_id,
                    "executive_id": executive_id,
                    "change_kind": action.removeprefix("access."),
                    "old_authorization_version": old_version,
                    "new_authorization_version": new_version,
                    **details,
                },
            ),
        )
