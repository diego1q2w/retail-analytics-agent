"""Read side of the access-change audit trail."""

from __future__ import annotations

import sqlalchemy as sa

from retail_analytics.adapters.postgres.database import Database
from retail_analytics.adapters.postgres.schema import audit_events
from retail_analytics.application.contracts.access_audit import (
    ACCESS_ACTIONS,
    SUBJECT_TYPE,
    AccessChange,
)


class PostgresAccessChangeHistory:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def history(self, executive_id: str, *, limit: int) -> list[AccessChange]:
        return await self._db.transaction(self._history, executive_id, limit)

    @staticmethod
    def _history(
        connection: sa.Connection, executive_id: str, limit: int
    ) -> list[AccessChange]:
        rows = connection.execute(
            sa.select(audit_events)
            .where(
                audit_events.c.subject_type == SUBJECT_TYPE,
                audit_events.c.subject_id == executive_id,
                audit_events.c.action.in_(ACCESS_ACTIONS),
            )
            .order_by(audit_events.c.occurred_at.desc(), audit_events.c.audit_id.desc())
            .limit(limit)
        )
        return [
            AccessChange(
                audit_id=r.audit_id,
                occurred_at=r.occurred_at,
                actor_id=r.actor_id,
                executive_id=r.subject_id,
                action=r.action,
                details=dict(r.details),
            )
            for r in rows
        ]
