"""Append to the durable audit trail inside the caller's transaction.

Used by adapters whose sensitive action must commit together with its audit
event: if the insert fails, the caller's whole transaction rolls back.
"""

from __future__ import annotations

import sqlalchemy as sa

from retail_analytics.adapters.postgres.schema import audit_events
from retail_analytics.application.contracts.audit import AuditEvent


def append_audit(connection: sa.Connection, event: AuditEvent) -> None:
    connection.execute(
        sa.insert(audit_events).values(
            audit_id=event.audit_id,
            occurred_at=event.occurred_at,
            actor_id=event.actor_id,
            action=event.action,
            subject_type=event.subject_type,
            subject_id=event.subject_id,
            session_id=event.session_id,
            run_id=event.run_id,
            details=event.details,
        )
    )
