from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """A sensitive action for the durable trail.

    ``details`` holds identifiers, versions and counts only: never report
    text, titles, query text or other content.
    """

    audit_id: str
    occurred_at: datetime
    actor_id: str
    action: str
    subject_type: str
    subject_id: str
    session_id: str | None = None
    run_id: str | None = None
    details: dict[str, object] = field(default_factory=dict)
