"""Several warehouse job submissions per query operation.

A query operation may need a second job when its first job ended without a
result for a transient reason, or when current authority changed before the
first job's result could be released. Each submission keeps its own job
reference (``submission`` 1, 2, ...), recorded before that job is submitted;
earlier references stay for reconciliation and diagnostics.

Revision ID: 0008
Revises: 0007
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "query_executions",
        sa.Column("submission", sa.Integer(), nullable=False, server_default="1"),
    )
    op.alter_column("query_executions", "submission", server_default=None)
    op.create_check_constraint(
        "ck_query_executions_submission", "query_executions", "submission >= 1"
    )
    op.drop_constraint("query_executions_pkey", "query_executions", type_="primary")
    op.create_primary_key(
        "query_executions_pkey", "query_executions", ["operation_id", "submission"]
    )


def downgrade() -> None:
    op.execute("DELETE FROM query_executions WHERE submission > 1")
    op.drop_constraint("query_executions_pkey", "query_executions", type_="primary")
    op.create_primary_key("query_executions_pkey", "query_executions", ["operation_id"])
    op.drop_constraint(
        "ck_query_executions_submission", "query_executions", type_="check"
    )
    op.drop_column("query_executions", "submission")
