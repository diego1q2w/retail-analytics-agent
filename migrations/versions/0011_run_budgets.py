"""Persistent run budgets.

``run_budgets`` holds one investigation's pinned limits and cumulative usage
(active time, provider requests, tokens, queries, bytes); ``budget_charges``
records each charged unit once per (run, kind, key), which makes charges
idempotent under activity retries. Reservations lock the run's budget row, so
concurrent charges of one run are serialized and can never jointly exceed a
limit. No foreign key to ``runs``, like the other run-scoped tables: retention
removes them together.

Revision ID: 0011
Revises: 0010
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "run_budgets",
        sa.Column("run_id", sa.Text(), primary_key=True),
        sa.Column("limits", JSONB(), nullable=False),
        sa.Column("active_seconds_used", sa.Float(53), nullable=False),
        sa.Column("active_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_requests", sa.Integer(), nullable=False),
        sa.Column("tokens", sa.BigInteger(), nullable=False),
        sa.Column("queries", sa.Integer(), nullable=False),
        sa.Column("bytes", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "active_seconds_used >= 0 AND provider_requests >= 0 AND tokens >= 0 "
            "AND queries >= 0 AND bytes >= 0",
            name="ck_run_budgets_usage",
        ),
    )
    op.create_table(
        "budget_charges",
        sa.Column("run_id", sa.Text(), primary_key=True),
        sa.Column("kind", sa.Text(), primary_key=True),
        sa.Column("charge_key", sa.Text(), primary_key=True),
        sa.Column("group_key", sa.Text(), nullable=True),
        sa.Column("bytes", sa.BigInteger(), nullable=False),
        sa.Column("tokens", sa.BigInteger(), nullable=False),
        sa.Column("settled", sa.Boolean(), nullable=False),
        sa.Column("ambiguous", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('query', 'provider_request', 'correction')",
            name="ck_budget_charges_kind",
        ),
        sa.CheckConstraint(
            "bytes >= 0 AND tokens >= 0", name="ck_budget_charges_amounts"
        ),
        sa.CheckConstraint(
            "(kind = 'correction') = (group_key IS NOT NULL)",
            name="ck_budget_charges_group",
        ),
    )
    op.create_index(
        "ix_budget_charges_group",
        "budget_charges",
        ["run_id", "kind", "group_key"],
    )


def downgrade() -> None:
    op.drop_index("ix_budget_charges_group", table_name="budget_charges")
    op.drop_table("budget_charges")
    op.drop_table("run_budgets")
