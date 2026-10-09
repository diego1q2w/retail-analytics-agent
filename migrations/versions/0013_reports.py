"""Saved reports: ownership, immutable versions and cited evidence.

- ``reports``: one row per report with its owner, originating conversation
  (no foreign key: the session may be cleaned up while the report lives) and
  ``deleted_at`` for soft deletion; everything else about a report is a version.
- ``report_versions``: immutable (trigger). Each points at the artifact version
  holding the Markdown, which must exist, and records the product-set digest
  the cited evidence was computed under so access can be judged on every read.
  ``(owner_id, idempotency_key)`` is unique: a retried save returns the
  original version. ``owner_id`` is tied to the report's owner.
- ``report_evidence``: evidence cited by a version (immutable). Evidence
  cannot be deleted while a report cites it.

Revision ID: 0013
Revises: 0012
"""

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY = ("report_versions", "report_evidence")


def _ts(name: str, *, nullable: bool = False) -> sa.Column[datetime]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "reports",
        sa.Column("report_id", sa.Text(), primary_key=True),
        sa.Column(
            "owner_id",
            sa.Text(),
            sa.ForeignKey("executives.executive_id"),
            nullable=False,
        ),
        sa.Column("session_id", sa.Text()),
        _ts("created_at"),
        _ts("deleted_at", nullable=True),
        sa.UniqueConstraint("report_id", "owner_id", name="uq_reports_owner"),
        sa.CheckConstraint("report_id ~ '^[0-9a-f]{32}$'", name="ck_reports_id"),
    )
    op.create_index("ix_reports_owner_created", "reports", ["owner_id", "created_at"])
    op.create_index("ix_reports_session", "reports", ["session_id"])

    op.create_table(
        "report_versions",
        sa.Column("report_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.Text(), nullable=False),
        sa.Column("artifact_version", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text()),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("draft_digest", sa.String(64), nullable=False),
        sa.Column("scope_digest", sa.String(64), nullable=False),
        sa.Column("authorization_version", sa.Integer(), nullable=False),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("report_id", "version"),
        sa.ForeignKeyConstraint(
            ["report_id", "owner_id"], ["reports.report_id", "reports.owner_id"]
        ),
        sa.ForeignKeyConstraint(
            ["report_id", "artifact_version"],
            ["artifact_versions.artifact_id", "artifact_versions.version"],
        ),
        sa.UniqueConstraint(
            "owner_id", "idempotency_key", name="uq_report_versions_idempotency"
        ),
        sa.CheckConstraint("version >= 1", name="ck_report_versions_version"),
        sa.CheckConstraint(
            "char_length(title) BETWEEN 1 AND 200", name="ck_report_versions_title"
        ),
    )

    op.create_table(
        "report_evidence",
        sa.Column("report_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "evidence_id",
            sa.Text(),
            sa.ForeignKey("evidence.evidence_id"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("report_id", "version", "evidence_id"),
        sa.ForeignKeyConstraint(
            ["report_id", "version"],
            ["report_versions.report_id", "report_versions.version"],
        ),
    )
    op.create_index("ix_report_evidence_evidence", "report_evidence", ["evidence_id"])

    # reject_history_update() was created by revision 0002.
    for table in APPEND_ONLY:
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_history_update()"
        )


def downgrade() -> None:
    for table in APPEND_ONLY:
        op.execute(f"DROP TRIGGER {table}_append_only ON {table}")
    op.drop_table("report_evidence")
    op.drop_table("report_versions")
    op.drop_table("reports")
