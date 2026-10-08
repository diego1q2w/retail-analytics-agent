"""PostgreSQL store for session topic boundaries."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from retail_analytics.adapters.postgres.database import Database
from retail_analytics.adapters.postgres.schema import topic_resets as tr
from retail_analytics.application.context import TopicReset
from retail_analytics.application.persistence import IdempotencyConflict


class PostgresTopicResets:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def record_reset(self, session_id: str, reset_id: str) -> TopicReset:
        return await self._db.transaction(self._record, session_id, reset_id)

    async def latest_reset(self, session_id: str) -> TopicReset | None:
        return await self._db.transaction(self._latest, session_id)

    def _record(
        self, connection: sa.Connection, session_id: str, reset_id: str
    ) -> TopicReset:
        connection.execute(
            insert(tr)
            .values(reset_id=reset_id, session_id=session_id, reset_at=self._db.clock())
            .on_conflict_do_nothing(index_elements=[tr.c.reset_id])
        )
        row = connection.execute(sa.select(tr).where(tr.c.reset_id == reset_id)).one()
        if row.session_id != session_id:
            raise IdempotencyConflict("topic_reset", reset_id)
        return TopicReset(row.session_id, row.reset_id, row.reset_at)

    @staticmethod
    def _latest(connection: sa.Connection, session_id: str) -> TopicReset | None:
        row = connection.execute(
            sa.select(tr)
            .where(tr.c.session_id == session_id)
            .order_by(tr.c.reset_at.desc(), tr.c.reset_id.desc())
            .limit(1)
        ).one_or_none()
        return (
            None
            if row is None
            else TopicReset(row.session_id, row.reset_id, row.reset_at)
        )
