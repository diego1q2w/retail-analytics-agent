"""Which execution backend owns a run (A03).

- ``execution_backend``: ``temporal`` (a durable workflow per run) or
  ``local`` (a task of the API process). Set when the run is created; a
  backend never starts, stops or takes over another backend's runs. Every
  existing run was created for Temporal, so the column is backfilled as
  ``temporal`` (server default) and history is unchanged.
- ``local_execution_id``: the manager process instance that owns a local run.
  Local runs never carry Temporal workflow IDs, and Temporal runs never carry a
  local execution ID (``ck_runs_execution_ids``).
- ``ix_runs_active_local``: the startup sweep finds active local runs left
  by a process that ended.

Revision ID: 0019
Revises: 0018
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACTIVE = "status IN ('running', 'waiting_for_input', 'cancelling')"


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column(
            "execution_backend",
            sa.Text(),
            server_default=sa.text("'temporal'"),
            nullable=False,
        ),
    )
    op.add_column("runs", sa.Column("local_execution_id", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_runs_execution_backend",
        "runs",
        "execution_backend IN ('temporal', 'local')",
    )
    op.create_check_constraint(
        "ck_runs_execution_ids",
        "runs",
        "(execution_backend = 'temporal' AND local_execution_id IS NULL) OR "
        "(execution_backend = 'local' AND temporal_workflow_id IS NULL "
        "AND temporal_run_id IS NULL)",
    )
    op.create_index(
        "ix_runs_active_local",
        "runs",
        ["session_id"],
        postgresql_where=sa.text(f"execution_backend = 'local' AND {_ACTIVE}"),
    )


def downgrade() -> None:
    # Local runs cannot be represented without the column: refuse rather
    # than silently re-label them as Temporal runs.
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM runs WHERE execution_backend = "
        "'local') THEN RAISE EXCEPTION 'local runs exist; cannot downgrade "
        "below 0019'; END IF; END $$"
    )
    op.drop_index("ix_runs_active_local", table_name="runs")
    op.drop_constraint("ck_runs_execution_ids", "runs", type_="check")
    op.drop_constraint("ck_runs_execution_backend", "runs", type_="check")
    op.drop_column("runs", "local_execution_id")
    op.drop_column("runs", "execution_backend")
