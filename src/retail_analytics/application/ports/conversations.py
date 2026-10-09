from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from retail_analytics.domain.conversation import Message, Session
from retail_analytics.domain.runs import Run


class ConversationReader(Protocol):
    """Read projections of sessions, runs and their released messages.

    Callers authorize first; these reads only filter by the IDs given.
    """

    async def sessions_for(
        self, executive_id: str, *, limit: int, offset: int
    ) -> Sequence[Session]:
        """The executive's sessions, most recently active first."""
        ...

    async def runs_in_session(self, session_id: str, *, limit: int) -> Sequence[Run]:
        """The session's runs, newest first."""
        ...

    async def message(self, message_id: str) -> Message | None: ...

    async def run_answer(self, run_id: str) -> Message | None:
        """The run's latest assistant message that is not a clarification
        question (its answer or partial findings), if any."""
        ...
