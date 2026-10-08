"""SQLAlchemy Core table descriptions used by the PostgreSQL adapters.

Migrations own the schema (``migrations/versions``); these descriptions only
name the columns queries use and must stay in step with the latest revision.
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

metadata = sa.MetaData()


def _ts(name: str, *, nullable: bool = False) -> sa.Column[datetime]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


sessions = sa.Table(
    "sessions",
    metadata,
    sa.Column("session_id", sa.Text, primary_key=True),
    sa.Column("executive_id", sa.Text, nullable=False),
    _ts("created_at"),
    _ts("last_activity_at"),
)

messages = sa.Table(
    "messages",
    metadata,
    sa.Column("message_id", sa.Text, primary_key=True),
    sa.Column("position", sa.BigInteger, sa.Identity()),
    sa.Column("session_id", sa.Text, nullable=False),
    sa.Column("run_id", sa.Text),
    sa.Column("role", sa.Text, nullable=False),
    sa.Column("content", sa.Text, nullable=False),
    _ts("created_at"),
)

runs = sa.Table(
    "runs",
    metadata,
    sa.Column("run_id", sa.Text, primary_key=True),
    sa.Column("session_id", sa.Text, nullable=False),
    sa.Column("requested_by", sa.Text, nullable=False),
    sa.Column("trigger_message_id", sa.Text, nullable=False),
    sa.Column("submission_key", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("temporal_workflow_id", sa.Text),
    sa.Column("temporal_run_id", sa.Text),
    sa.Column("last_event_sequence", sa.Integer, nullable=False),
    _ts("created_at"),
    _ts("updated_at"),
    _ts("completed_at", nullable=True),
)

tool_executions = sa.Table(
    "tool_executions",
    metadata,
    sa.Column("operation_id", sa.Text, primary_key=True),
    sa.Column("run_id", sa.Text, nullable=False),
    sa.Column("capability", sa.Text, nullable=False),
    sa.Column("capability_version", sa.Integer, nullable=False),
    sa.Column("side_effect", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("attempt_count", sa.Integer, nullable=False),
    sa.Column("error_code", sa.Text),
    sa.Column("error_detail", sa.String(280)),
    _ts("deadline_at", nullable=True),
    _ts("created_at"),
    _ts("updated_at"),
)

query_executions = sa.Table(
    "query_executions",
    metadata,
    sa.Column("operation_id", sa.Text, primary_key=True),
    sa.Column("job_id", sa.Text, nullable=False),
    sa.Column("project", sa.Text, nullable=False),
    sa.Column("location", sa.Text, nullable=False),
    sa.Column("query_fingerprint", sa.Text, nullable=False),
    sa.Column("query_ref", sa.Text, nullable=False),
    sa.Column("authorization_version", sa.Integer, nullable=False),
    sa.Column("catalog_version", sa.Text, nullable=False),
    _ts("registered_at"),
)

execution_events = sa.Table(
    "execution_events",
    metadata,
    sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
    sa.Column("operation_id", sa.Text, nullable=False),
    sa.Column("sequence", sa.Integer, nullable=False),
    sa.Column("from_status", sa.Text),
    sa.Column("to_status", sa.Text, nullable=False),
    sa.Column("attempt", sa.Integer, nullable=False),
    sa.Column("error_code", sa.Text),
    sa.Column("detail", sa.String(280)),
    _ts("occurred_at"),
)

run_events = sa.Table(
    "run_events",
    metadata,
    sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
    sa.Column("event_id", sa.Text, nullable=False),
    sa.Column("run_id", sa.Text, nullable=False),
    sa.Column("sequence", sa.Integer, nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("payload", JSONB, nullable=False),
    _ts("occurred_at"),
)

executives = sa.Table(
    "executives",
    metadata,
    sa.Column("executive_id", sa.Text, primary_key=True),
    sa.Column("issuer", sa.Text, nullable=False),
    sa.Column("subject", sa.Text, nullable=False),
    sa.Column("label", sa.String(120), nullable=False),
    sa.Column("roles", ARRAY(sa.Text), nullable=False),
    sa.Column("active", sa.Boolean, nullable=False),
    sa.Column("authorization_version", sa.Integer, nullable=False),
    _ts("created_at"),
    _ts("updated_at"),
)

product_entitlements = sa.Table(
    "product_entitlements",
    metadata,
    sa.Column("executive_id", sa.Text, primary_key=True),
    sa.Column("product_id", sa.Text, primary_key=True),
    _ts("granted_at"),
)
