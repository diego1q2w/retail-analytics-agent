"""Topic boundaries of sessions.

``topic_resets`` records when a user started a new topic in a session. Context
assembly excludes earlier messages and evidence from model context; nothing
is deleted, so saved reports and retained evidence are unaffected. Rows hold
no message text and go with their session.

Revision ID: 0010
Revises: 0009
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "topic_resets",
        sa.Column("reset_id", sa.Text(), primary_key=True),
        sa.Column(
            "session_id",
            sa.Text(),
            sa.ForeignKey("sessions.session_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("reset_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_topic_resets_session_time", "topic_resets", ["session_id", "reset_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_topic_resets_session_time", table_name="topic_resets")
    op.drop_table("topic_resets")
