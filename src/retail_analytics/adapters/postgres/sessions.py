"""PostgreSQL ``SessionRepository``."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from retail_analytics.adapters.postgres.database import Database
from retail_analytics.adapters.postgres.schema import messages, sessions
from retail_analytics.application.contracts.persistence import (
    IdempotencyConflict,
    RecordNotFound,
)
from retail_analytics.domain.conversation import Message, MessageRole, Session


def session_from_row(row: sa.Row[tuple[object, ...]]) -> Session:
    m = row._mapping
    return Session(
        session_id=m["session_id"],
        executive_id=m["executive_id"],
        created_at=m["created_at"],
        last_activity_at=m["last_activity_at"],
    )


def message_from_row(row: sa.Row[tuple[object, ...]]) -> Message:
    m = row._mapping
    return Message(
        message_id=m["message_id"],
        session_id=m["session_id"],
        role=MessageRole(m["role"]),
        content=m["content"],
        created_at=m["created_at"],
        run_id=m["run_id"],
    )


def insert_message(
    connection: sa.Connection,
    *,
    message_id: str,
    session_id: str,
    role: MessageRole,
    content: str,
    run_id: str | None,
    at: datetime,
) -> Message:
    """Insert once by message ID; an identical repeat returns the original."""
    inserted = connection.execute(
        insert(messages)
        .values(
            message_id=message_id,
            session_id=session_id,
            run_id=run_id,
            role=role.value,
            content=content,
            created_at=at,
        )
        .on_conflict_do_nothing(index_elements=["message_id"])
        .returning(*messages.c)
    ).one_or_none()
    if inserted is not None:
        if role is MessageRole.USER:
            # User interaction extends retention; produced output does not.
            connection.execute(
                sa.update(sessions)
                .where(sessions.c.session_id == session_id)
                .values(last_activity_at=at)
            )
        return message_from_row(inserted)
    existing = message_from_row(
        connection.execute(
            sa.select(messages).where(messages.c.message_id == message_id)
        ).one()
    )
    if (existing.session_id, existing.role, existing.content, existing.run_id) != (
        session_id,
        role,
        content,
        run_id,
    ):
        raise IdempotencyConflict("message", message_id)
    return existing


class PostgresSessionRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create_session(self, session_id: str, executive_id: str) -> Session:
        return await self._db.transaction(self._create, session_id, executive_id)

    def _create(
        self, connection: sa.Connection, session_id: str, executive_id: str
    ) -> Session:
        now = self._db.clock()
        row = connection.execute(
            insert(sessions)
            .values(
                session_id=session_id,
                executive_id=executive_id,
                created_at=now,
                last_activity_at=now,
            )
            .on_conflict_do_nothing(index_elements=["session_id"])
            .returning(*sessions.c)
        ).one_or_none()
        if row is None:
            row = connection.execute(
                sa.select(sessions).where(sessions.c.session_id == session_id)
            ).one()
        session = session_from_row(row)
        if session.executive_id != executive_id:
            raise IdempotencyConflict("session", session_id)
        return session

    async def get_session(self, session_id: str) -> Session | None:
        return await self._db.transaction(self._get, session_id)

    @staticmethod
    def _get(connection: sa.Connection, session_id: str) -> Session | None:
        row = connection.execute(
            sa.select(sessions).where(sessions.c.session_id == session_id)
        ).one_or_none()
        return None if row is None else session_from_row(row)

    async def append_message(
        self,
        *,
        message_id: str,
        session_id: str,
        role: MessageRole,
        content: str,
        run_id: str | None = None,
    ) -> Message:
        return await self._db.transaction(
            self._append, message_id, session_id, role, content, run_id
        )

    def _append(
        self,
        connection: sa.Connection,
        message_id: str,
        session_id: str,
        role: MessageRole,
        content: str,
        run_id: str | None,
    ) -> Message:
        if self._get(connection, session_id) is None:
            raise RecordNotFound("session", session_id)
        return insert_message(
            connection,
            message_id=message_id,
            session_id=session_id,
            role=role,
            content=content,
            run_id=run_id,
            at=self._db.clock(),
        )

    async def recent_messages(self, session_id: str, limit: int) -> Sequence[Message]:
        return await self._db.transaction(self._recent, session_id, limit)

    @staticmethod
    def _recent(
        connection: sa.Connection, session_id: str, limit: int
    ) -> list[Message]:
        rows = connection.execute(
            sa.select(messages)
            .where(messages.c.session_id == session_id)
            .order_by(messages.c.position.desc())
            .limit(limit)
        ).all()
        return [message_from_row(row) for row in reversed(rows)]
