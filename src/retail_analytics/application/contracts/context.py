from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class TopicReset:
    session_id: str
    reset_id: str
    reset_at: datetime
