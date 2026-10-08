"""Sessions and their messages: the owned conversation spanning follow-ups."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True, slots=True)
class Session:
    """A conversation owned by one executive.

    ``last_activity_at`` moves on user interaction and run completion only;
    background progress does not extend retention.
    """

    session_id: str
    executive_id: str
    created_at: datetime
    last_activity_at: datetime


@dataclass(frozen=True, slots=True)
class Message:
    message_id: str
    session_id: str
    role: MessageRole
    content: str
    created_at: datetime
    # Set for messages produced within a run (e.g. the assistant's answer).
    run_id: str | None = None
