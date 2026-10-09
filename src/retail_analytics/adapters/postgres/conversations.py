"""PostgreSQL ``ConversationReader``: read projections for client views."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from retail_analytics.adapters.postgres.database import Database
from retail_analytics.adapters.postgres.runs import run_from_row
from retail_analytics.adapters.postgres.schema import (
    messages,
    run_questions,
    runs,
    sessions,
)
from retail_analytics.adapters.postgres.sessions import (
    message_from_row,
    session_from_row,
)
from retail_analytics.domain.conversation import Message, MessageRole, Session
from retail_analytics.domain.runs import Run


class PostgresConversationReader:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def sessions_for(
        self, executive_id: str, *, limit: int, offset: int
    ) -> Sequence[Session]:
        return await self._db.transaction(
            self._sessions_for, executive_id, limit, offset
        )

    @staticmethod
    def _sessions_for(
        connection: sa.Connection, executive_id: str, limit: int, offset: int
    ) -> list[Session]:
        rows = connection.execute(
            sa.select(sessions)
            .where(sessions.c.executive_id == executive_id)
            .order_by(sessions.c.last_activity_at.desc(), sessions.c.session_id)
            .limit(limit)
            .offset(offset)
        ).all()
        return [session_from_row(row) for row in rows]

    async def runs_in_session(self, session_id: str, *, limit: int) -> Sequence[Run]:
        return await self._db.transaction(self._runs_in_session, session_id, limit)

    @staticmethod
    def _runs_in_session(
        connection: sa.Connection, session_id: str, limit: int
    ) -> list[Run]:
        rows = connection.execute(
            sa.select(runs)
            .where(runs.c.session_id == session_id)
            .order_by(runs.c.created_at.desc(), runs.c.run_id)
            .limit(limit)
        ).all()
        return [run_from_row(row) for row in rows]

    async def message(self, message_id: str) -> Message | None:
        return await self._db.transaction(self._message, message_id)

    @staticmethod
    def _message(connection: sa.Connection, message_id: str) -> Message | None:
        row = connection.execute(
            sa.select(messages).where(messages.c.message_id == message_id)
        ).one_or_none()
        return None if row is None else message_from_row(row)

    async def run_answer(self, run_id: str) -> Message | None:
        return await self._db.transaction(self._run_answer, run_id)

    @staticmethod
    def _run_answer(connection: sa.Connection, run_id: str) -> Message | None:
        questions = sa.select(run_questions.c.message_id).where(
            run_questions.c.run_id == run_id
        )
        row = connection.execute(
            sa.select(messages)
            .where(
                messages.c.run_id == run_id,
                messages.c.role == MessageRole.ASSISTANT.value,
                messages.c.message_id.not_in(questions),
            )
            .order_by(messages.c.position.desc())
            .limit(1)
        ).one_or_none()
        return None if row is None else message_from_row(row)
