"""Estimated model spend per run (T30-F3).

``run_budgets`` gains the run's accumulated estimated model spend
(``model_cost_micros``, micro-USD) and the count of settled provider requests
whose price was unknown (``unpriced_requests``). ``budget_charges`` gains each
provider request's estimated cost (NULL while unsettled or when the price is
unknown) and ``detail``: the normalized usage categories and price basis, for
audit (numbers and codes only, never content). Existing rows get zero spend;
their pinned limits carry no dollar limit.

Revision ID: 0021
Revises: 0020
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "run_budgets",
        sa.Column(
            "model_cost_micros", sa.BigInteger(), nullable=False, server_default="0"
        ),
    )
    op.add_column(
        "run_budgets",
        sa.Column(
            "unpriced_requests", sa.Integer(), nullable=False, server_default="0"
        ),
    )
    op.create_check_constraint(
        "ck_run_budgets_model_cost",
        "run_budgets",
        "model_cost_micros >= 0 AND unpriced_requests >= 0",
    )
    op.add_column("budget_charges", sa.Column("cost_micros", sa.BigInteger()))
    op.add_column("budget_charges", sa.Column("detail", JSONB()))
    op.create_check_constraint(
        "ck_budget_charges_cost",
        "budget_charges",
        "cost_micros IS NULL OR (kind = 'provider_request' AND cost_micros >= 0)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_budget_charges_cost", "budget_charges", type_="check")
    op.drop_column("budget_charges", "detail")
    op.drop_column("budget_charges", "cost_micros")
    op.drop_constraint("ck_run_budgets_model_cost", "run_budgets", type_="check")
    op.drop_column("run_budgets", "unpriced_requests")
    op.drop_column("run_budgets", "model_cost_micros")
