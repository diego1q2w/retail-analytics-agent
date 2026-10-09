"""PostgreSQL ``ProductScopeSnapshots``: exact product sets keyed by digest.

A snapshot is written in the same transaction as the evidence whose authority
stamp names its digest, from the trusted execution context. Rows are immutable
and a CHECK constraint ties the digest to the stored (sorted) IDs, so a digest
always means exactly one product set. Product IDs are only ever compared in
SQL (``<@``); they are never returned to callers.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import insert as pg_insert

from retail_analytics.adapters.postgres.database import Database
from retail_analytics.adapters.postgres.schema import product_scope_snapshots as ps
from retail_analytics.domain.access import ProductScope
from retail_analytics.domain.evidence import product_set_digest


def record_snapshot(
    connection: sa.Connection, product_ids: Iterable[str], now: datetime
) -> str:
    """Store the exact set (once per digest) and return its digest."""
    ids = sorted(set(product_ids))
    digest = product_set_digest(ids)
    known = connection.execute(
        sa.select(ps.c.scope_digest).where(ps.c.scope_digest == digest)
    ).first()
    if known is None:
        connection.execute(
            pg_insert(ps)
            .values(scope_digest=digest, product_ids=ids, recorded_at=now)
            .on_conflict_do_nothing(index_elements=[ps.c.scope_digest])
        )
    return digest


class PostgresProductScopeSnapshots:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def combine(self, digests: Collection[str]) -> str | None:
        return await self._db.transaction(self._combine, frozenset(digests))

    async def covered(
        self, digests: Collection[str], scope: ProductScope
    ) -> frozenset[str]:
        wanted = frozenset(digests)
        if not wanted or scope.is_empty:
            return frozenset()
        return await self._db.transaction(
            self._covered, wanted, sorted(scope.product_ids)
        )

    def _combine(
        self, connection: sa.Connection, digests: frozenset[str]
    ) -> str | None:
        if not digests:
            return None
        rows = connection.execute(
            sa.select(ps.c.scope_digest, ps.c.product_ids).where(
                ps.c.scope_digest.in_(sorted(digests))
            )
        ).all()
        if len(rows) != len(digests):
            return None
        if len(rows) == 1:
            return str(rows[0].scope_digest)
        union: set[str] = set()
        for row in rows:
            union.update(row.product_ids)
        return record_snapshot(connection, union, self._db.clock())

    @staticmethod
    def _covered(
        connection: sa.Connection, digests: frozenset[str], current: list[str]
    ) -> frozenset[str]:
        allowed = sa.bindparam("current", current, type_=ARRAY(sa.Text))
        rows = connection.execute(
            sa.select(ps.c.scope_digest).where(
                ps.c.scope_digest.in_(sorted(digests)),
                ps.c.product_ids.contained_by(allowed),
            )
        ).scalars()
        return frozenset(rows)
