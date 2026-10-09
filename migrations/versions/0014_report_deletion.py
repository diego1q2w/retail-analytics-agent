"""Exact report-deletion proposals and the durable audit trail.

- ``deletion_proposals``: one pending confirmation per proposal. A proposal
  names its requester, expiry and request digest and moves out of ``pending``
  exactly once (a trigger rejects any later change), so a proposal is
  single-use however it is replayed. ``(owner_id, idempotency_key)`` is unique:
  a retried proposal returns the original.
- ``deletion_proposal_items``: the exact report IDs and the version each one
  had when proposed (immutable). Reports created or changed later are never
  part of the proposal.
- ``audit_events``: durable accountability for sensitive actions, append-only.
  Details hold identifiers and counts only, never report content or titles.

Revision ID: 0014
Revises: 0013
"""

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _ts(name: str, *, nullable: bool = False) -> sa.Column[datetime]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "deletion_proposals",
        sa.Column("proposal_id", sa.Text(), primary_key=True),
        sa.Column(
            "owner_id",
            sa.Text(),
            sa.ForeignKey("executives.executive_id"),
            nullable=False,
        ),
        sa.Column("session_id", sa.Text()),
        sa.Column("run_id", sa.Text()),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        _ts("created_at"),
        _ts("expires_at"),
        _ts("resolved_at", nullable=True),
        sa.UniqueConstraint(
            "owner_id", "idempotency_key", name="uq_deletion_proposals_idempotency"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'confirmed', 'cancelled')",
            name="ck_deletion_proposals_status",
        ),
        sa.CheckConstraint(
            "(status = 'pending') = (resolved_at IS NULL)",
            name="ck_deletion_proposals_resolved",
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name="ck_deletion_proposals_expiry"
        ),
    )
    op.create_index(
        "ix_deletion_proposals_pending",
        "deletion_proposals",
        ["owner_id"],
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.create_table(
        "deletion_proposal_items",
        sa.Column(
            "proposal_id",
            sa.Text(),
            sa.ForeignKey("deletion_proposals.proposal_id"),
            nullable=False,
        ),
        sa.Column("report_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("proposal_id", "report_id"),
        sa.ForeignKeyConstraint(
            ["report_id", "version"],
            ["report_versions.report_id", "report_versions.version"],
        ),
    )
    op.create_index(
        "ix_deletion_proposal_items_report", "deletion_proposal_items", ["report_id"]
    )

    op.create_table(
        "audit_events",
        sa.Column("audit_id", sa.Text(), primary_key=True),
        _ts("occurred_at"),
        # No foreign key: the trail must outlive the records it describes.
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("subject_type", sa.Text(), nullable=False),
        sa.Column("subject_id", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Text()),
        sa.Column("run_id", sa.Text()),
        sa.Column(
            "details",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_index(
        "ix_audit_events_subject", "audit_events", ["subject_type", "subject_id"]
    )
    op.create_index(
        "ix_audit_events_actor", "audit_events", ["actor_id", "occurred_at"]
    )

    # reject_history_update() was created by revision 0002.
    for table in ("deletion_proposal_items", "audit_events"):
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_history_update()"
        )
    op.execute(
        """
        CREATE FUNCTION deletion_proposals_single_use() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.status <> 'pending'
               OR NEW.proposal_id IS DISTINCT FROM OLD.proposal_id
               OR NEW.owner_id IS DISTINCT FROM OLD.owner_id
               OR NEW.request_digest IS DISTINCT FROM OLD.request_digest
               OR NEW.created_at IS DISTINCT FROM OLD.created_at
               OR NEW.expires_at IS DISTINCT FROM OLD.expires_at THEN
                RAISE EXCEPTION 'a resolved deletion proposal cannot change'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER deletion_proposals_single_use BEFORE UPDATE "
        "ON deletion_proposals FOR EACH ROW "
        "EXECUTE FUNCTION deletion_proposals_single_use()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER deletion_proposals_single_use ON deletion_proposals")
    op.execute("DROP FUNCTION deletion_proposals_single_use()")
    for table in ("deletion_proposal_items", "audit_events"):
        op.execute(f"DROP TRIGGER {table}_append_only ON {table}")
    op.drop_table("audit_events")
    op.drop_table("deletion_proposal_items")
    op.drop_table("deletion_proposals")
