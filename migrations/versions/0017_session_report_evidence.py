"""Reuse of the owner's saved-report evidence in their other sessions (T18-F2).

- ``session_report_evidence``: report evidence linked into another session of
  the same owner, with the report version it came from (source and title for
  disclosure). The link grants nothing: every use re-judges the record against
  the owner's current products (required-scope coverage, T18-F1). Rows go with
  their session or evidence. ``invalidated_at`` is set once when an analytical
  setting scoped to the importing session changes.

Revision ID: 0017
Revises: 0016
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "session_report_evidence",
        sa.Column(
            "session_id",
            sa.Text(),
            sa.ForeignKey("sessions.session_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "evidence_id",
            sa.Text(),
            sa.ForeignKey("evidence.evidence_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("executive_id", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("report_id", sa.Text(), nullable=False),
        sa.Column("report_version", sa.Integer(), nullable=False),
        sa.Column("report_title", sa.Text(), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("invalidated_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("report_version >= 1", name="ck_session_report_version"),
    )
    op.create_index(
        "ix_session_report_evidence_evidence",
        "session_report_evidence",
        ["evidence_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_session_report_evidence_evidence", table_name="session_report_evidence"
    )
    op.drop_table("session_report_evidence")
