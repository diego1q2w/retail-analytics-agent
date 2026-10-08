"""Investigation runtime records: who a run acts for and the input it received.

``run_principals`` keeps the authenticated principal (executive and token
scope ceiling) a run acts for, so retried activities re-resolve current
authority without the original token. ``run_inputs`` is the ordered log of
user input to a run (the request, steering messages, clarification answers)
and of requests queued behind a session's active run; the runtime reads it at
safe boundaries, never trusting workflow signal payloads. ``run_questions``
persists the clarification an investigation is waiting on, so a reconnecting
client finds it. Inputs hold user text like ``messages`` and share its
retention; no foreign key to ``runs``, like the other run-scoped tables.

Revision ID: 0012
Revises: 0011
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "run_principals",
        sa.Column("run_id", sa.Text(), primary_key=True),
        sa.Column("executive_id", sa.Text(), nullable=False),
        sa.Column("scopes", ARRAY(sa.Text()), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "run_inputs",
        sa.Column("input_id", sa.Text(), primary_key=True),
        sa.Column("position", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("message_id", sa.Text(), nullable=True),
        sa.Column("question_id", sa.Text(), nullable=True),
        sa.Column("promoted_run_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('request', 'steering', 'answer', 'queued')",
            name="ck_run_inputs_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'applied', 'promoted', 'discarded')",
            name="ck_run_inputs_status",
        ),
        sa.CheckConstraint(
            "(kind = 'queued') = (run_id IS NULL)", name="ck_run_inputs_run"
        ),
    )
    op.create_index("ix_run_inputs_run", "run_inputs", ["run_id", "position"])
    op.create_index(
        "ix_run_inputs_queued",
        "run_inputs",
        ["session_id", "position"],
        postgresql_where=sa.text("kind = 'queued' AND status = 'pending'"),
    )
    op.create_table(
        "run_questions",
        sa.Column("question_id", sa.Text(), primary_key=True),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("message_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("asked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('open', 'answered', 'closed')", name="ck_run_questions_status"
        ),
    )
    op.create_index(
        "ux_run_questions_one_open",
        "run_questions",
        ["run_id"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )


def downgrade() -> None:
    op.drop_index("ux_run_questions_one_open", table_name="run_questions")
    op.drop_table("run_questions")
    op.drop_index("ix_run_inputs_queued", table_name="run_inputs")
    op.drop_index("ix_run_inputs_run", table_name="run_inputs")
    op.drop_table("run_inputs")
    op.drop_table("run_principals")
