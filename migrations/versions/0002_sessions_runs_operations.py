"""Sessions, messages, runs, operations and ordered event histories.

- ``runs``: at most one active run per session (partial unique index), one run
  per (session, submission key), Temporal identifiers stored apart from the
  application run ID.
- ``tool_executions``: the operation ID is the primary and idempotency key;
  status here is authoritative. ``query_executions`` adds the warehouse job
  reference, one row per operation.
- ``execution_events`` and ``run_events`` are append-only (updates rejected by
  trigger) and ordered per operation / per run. Deletion is left to retention
  cleanup; no cascades, so unresolved records are never removed implicitly.

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUN_STATUSES = (
    "running",
    "waiting_for_input",
    "cancelling",
    "completed",
    "partial",
    "failed",
    "cancelled",
)
ACTIVE_RUN_STATUSES = ("running", "waiting_for_input", "cancelling")
OPERATION_STATUSES = (
    "prepared",
    "submitting",
    "running",
    "outcome_unknown",
    "retrying",
    "cancel_requested",
    "succeeded",
    "failed",
    "cancelled",
)
SIDE_EFFECTS = ("read_only", "idempotent_write", "external_job", "external_delivery")
APPEND_ONLY_TABLES = ("execution_events", "run_events")


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _timestamp(name: str, *, nullable: bool = False) -> sa.Column[sa.DateTime]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("session_id", sa.Text(), primary_key=True),
        sa.Column("executive_id", sa.Text(), nullable=False),
        _timestamp("created_at"),
        _timestamp("last_activity_at"),
    )
    op.create_index("ix_sessions_executive_id", "sessions", ["executive_id"])

    op.create_table(
        "messages",
        sa.Column("message_id", sa.Text(), primary_key=True),
        sa.Column("position", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column(
            "session_id",
            sa.Text(),
            sa.ForeignKey("sessions.session_id"),
            nullable=False,
        ),
        sa.Column("run_id", sa.Text(), nullable=True),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        _timestamp("created_at"),
        sa.CheckConstraint("role IN ('user', 'assistant')", name="ck_messages_role"),
        sa.UniqueConstraint("position", name="uq_messages_position"),
    )
    op.create_index(
        "ix_messages_session_position", "messages", ["session_id", "position"]
    )

    op.create_table(
        "runs",
        sa.Column("run_id", sa.Text(), primary_key=True),
        sa.Column(
            "session_id",
            sa.Text(),
            sa.ForeignKey("sessions.session_id"),
            nullable=False,
        ),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column(
            "trigger_message_id",
            sa.Text(),
            sa.ForeignKey("messages.message_id"),
            nullable=False,
        ),
        sa.Column("submission_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("temporal_workflow_id", sa.Text(), nullable=True),
        sa.Column("temporal_run_id", sa.Text(), nullable=True),
        sa.Column(
            "last_event_sequence",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        _timestamp("completed_at", nullable=True),
        sa.CheckConstraint(_in("status", RUN_STATUSES), name="ck_runs_status"),
        sa.UniqueConstraint(
            "session_id", "submission_key", name="uq_runs_session_submission"
        ),
        sa.UniqueConstraint("trigger_message_id", name="uq_runs_trigger_message"),
        sa.UniqueConstraint("temporal_workflow_id", name="uq_runs_temporal_workflow"),
    )
    op.create_index(
        "ux_runs_one_active_per_session",
        "runs",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text(_in("status", ACTIVE_RUN_STATUSES)),
    )
    op.create_foreign_key("fk_messages_run", "messages", "runs", ["run_id"], ["run_id"])

    op.create_table(
        "tool_executions",
        sa.Column("operation_id", sa.Text(), primary_key=True),
        sa.Column("run_id", sa.Text(), sa.ForeignKey("runs.run_id"), nullable=False),
        sa.Column("capability", sa.Text(), nullable=False),
        sa.Column("capability_version", sa.Integer(), nullable=False),
        sa.Column("side_effect", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_detail", sa.String(280), nullable=True),
        _timestamp("deadline_at", nullable=True),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.CheckConstraint(
            _in("status", OPERATION_STATUSES), name="ck_tool_executions_status"
        ),
        sa.CheckConstraint(
            _in("side_effect", SIDE_EFFECTS), name="ck_tool_executions_side_effect"
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name="ck_tool_executions_attempt_count"
        ),
    )
    op.create_index("ix_tool_executions_run_id", "tool_executions", ["run_id"])

    op.create_table(
        "query_executions",
        sa.Column(
            "operation_id",
            sa.Text(),
            sa.ForeignKey("tool_executions.operation_id"),
            primary_key=True,
        ),
        sa.Column("job_id", sa.Text(), nullable=False),
        sa.Column("project", sa.Text(), nullable=False),
        sa.Column("location", sa.Text(), nullable=False),
        sa.Column("query_fingerprint", sa.Text(), nullable=False),
        sa.Column("query_ref", sa.Text(), nullable=False),
        sa.Column("authorization_version", sa.Integer(), nullable=False),
        sa.Column("catalog_version", sa.Text(), nullable=False),
        _timestamp("registered_at"),
        sa.UniqueConstraint("project", "location", "job_id", name="uq_query_job"),
    )

    op.create_table(
        "execution_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "operation_id",
            sa.Text(),
            sa.ForeignKey("tool_executions.operation_id"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("from_status", sa.Text(), nullable=True),
        sa.Column("to_status", sa.Text(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("detail", sa.String(280), nullable=True),
        _timestamp("occurred_at"),
        sa.UniqueConstraint(
            "operation_id", "sequence", name="uq_execution_events_sequence"
        ),
    )

    op.create_table(
        "run_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text(), sa.ForeignKey("runs.run_id"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        _timestamp("occurred_at"),
        sa.UniqueConstraint("event_id", name="uq_run_events_event_id"),
        sa.UniqueConstraint("run_id", "sequence", name="uq_run_events_sequence"),
    )

    op.execute(
        """
        CREATE FUNCTION reject_history_update() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '% is append-only', TG_TABLE_NAME
                USING ERRCODE = 'restrict_violation';
        END;
        $$
        """
    )
    for table in APPEND_ONLY_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_history_update()"
        )


def downgrade() -> None:
    for table in APPEND_ONLY_TABLES:
        op.execute(f"DROP TRIGGER {table}_append_only ON {table}")
    op.execute("DROP FUNCTION reject_history_update()")
    op.drop_table("run_events")
    op.drop_table("execution_events")
    op.drop_table("query_executions")
    op.drop_table("tool_executions")
    op.drop_constraint("fk_messages_run", "messages", type_="foreignkey")
    op.drop_table("runs")
    op.drop_table("messages")
    op.drop_table("sessions")
