from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from retail_analytics.application.contracts.context import TopicReset
from retail_analytics.domain.conversation import Message


class TopicResets(Protocol):
    """Durable topic boundaries of sessions."""

    async def record_reset(self, session_id: str, reset_id: str) -> TopicReset:
        """Record once; repeating the same ``reset_id`` returns the original."""
        ...

    async def latest_reset(self, session_id: str) -> TopicReset | None: ...


class MessageHistory(Protocol):
    """Satisfied by ``SessionRepository``."""

    async def recent_messages(self, session_id: str, limit: int) -> Sequence[Message]:
        """The latest ``limit`` messages, oldest first."""
        ...
