"""PostgreSQL ``ExecutiveDirectory`` and ``AccessAdministration``.

Reads take the executive row and its product set in one statement, so the
version and the products always belong to the same committed state. Writes
lock the executive row, apply the change and increment
``authorization_version`` in one transaction; identical repeats change
nothing.
"""

from __future__ import annotations

from collections.abc import Iterable

import sqlalchemy as sa
from sqlalchemy import exc
from sqlalchemy.dialects.postgresql import insert

from retail_analytics.adapters.postgres.database import Database, violated_constraint
from retail_analytics.adapters.postgres.schema import executives, product_entitlements
from retail_analytics.application.authorization import ExecutiveRegistration
from retail_analytics.application.persistence import (
    IdempotencyConflict,
    RecordNotFound,
)
from retail_analytics.domain.access import ExecutiveAccess, Role, is_valid_product_id

_PRODUCTS = (
    sa.select(sa.func.array_agg(product_entitlements.c.product_id))
    .where(product_entitlements.c.executive_id == executives.c.executive_id)
    .scalar_subquery()
    .label("product_ids")
)


def _select() -> sa.Select[tuple[object, ...]]:
    return sa.select(
        executives.c.executive_id,
        executives.c.roles,
        executives.c.active,
        executives.c.authorization_version,
        _PRODUCTS,
    )


def _access(row: sa.Row[tuple[object, ...]]) -> ExecutiveAccess:
    m = row._mapping
    return ExecutiveAccess(
        executive_id=m["executive_id"],
        roles=frozenset(Role(role) for role in m["roles"]),
        product_ids=frozenset(m["product_ids"] or ()),
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
        self, registration: ExecutiveRegistration
    ) -> ExecutiveAccess:
        try:
            return await self._db.transaction(self._register, registration)
        except exc.IntegrityError as error:
            if violated_constraint(error) == "uq_executives_identity":
                raise IdempotencyConflict(
                    "executive identity", registration.executive_id
                ) from None
            raise

    def _register(
        self, connection: sa.Connection, registration: ExecutiveRegistration
    ) -> ExecutiveAccess:
        now = self._db.clock()
        roles = sorted(role.value for role in registration.roles)
        connection.execute(
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
        )
        current = _lock(connection, registration.executive_id)._mapping
        if (current["issuer"], current["subject"]) != (
            registration.issuer,
            registration.subject,
        ):
            raise IdempotencyConflict("executive", registration.executive_id)
        if (sorted(current["roles"]), current["label"]) != (roles, registration.label):
            self._bump(
                connection,
                registration.executive_id,
                roles=roles,
                label=registration.label,
            )
        return _load(connection, registration.executive_id)

    async def replace_products(
        self, executive_id: str, product_ids: Iterable[str]
    ) -> ExecutiveAccess:
        wanted = frozenset(product_ids)
        invalid = [p for p in wanted if not is_valid_product_id(p)]
        if invalid:
            raise ValueError(f"{len(invalid)} invalid product IDs")
        return await self._db.transaction(self._replace, executive_id, wanted)

    def _replace(
        self, connection: sa.Connection, executive_id: str, wanted: frozenset[str]
    ) -> ExecutiveAccess:
        _lock(connection, executive_id)
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
            self._bump(connection, executive_id)
        return _load(connection, executive_id)

    async def set_active(self, executive_id: str, active: bool) -> ExecutiveAccess:
        return await self._db.transaction(self._set_active, executive_id, active)

    def _set_active(
        self, connection: sa.Connection, executive_id: str, active: bool
    ) -> ExecutiveAccess:
        if _lock(connection, executive_id)._mapping["active"] != active:
            self._bump(connection, executive_id, active=active)
        return _load(connection, executive_id)

    def _bump(
        self, connection: sa.Connection, executive_id: str, **changes: object
    ) -> None:
        connection.execute(
            sa.update(executives)
            .where(executives.c.executive_id == executive_id)
            .values(
                **changes,
                authorization_version=executives.c.authorization_version + 1,
                updated_at=self._db.clock(),
            )
        )
