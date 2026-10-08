"""Immutable analytical evidence, its run usage, invalidations and pins.

- ``evidence``: one row per evidence version, never updated. Bounded JSONB
  payloads (released rows, provenance, analytical stamp) plus the columns
  reuse checks need: owner, session, authorization version and product-set
  digest, catalog/policy versions and preference fingerprint. A refresh is a
  new row in the same lineage with the next version. One row per producing
  operation, so a retried activity cannot store a second copy.
- ``evidence_dependencies``: ordered inputs of derived evidence. An input
  cannot be deleted while something still depends on it.
- ``run_evidence``: which runs produced or reused which evidence.
- ``evidence_invalidations``: append-only marks that a finding was computed
  under a meaning that has since changed (e.g. a metric preference).
- ``evidence_pins``: retention holds (saved reports). Pinned evidence cannot be
  deleted by investigation cleanup.

Revision ID: 0007
Revises: 0006
"""

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY = ("evidence", "evidence_dependencies", "evidence_invalidations")
# Hard ceilings above the application's limits (512 KiB rows, 64 KiB provenance).
MAX_PAYLOAD_TEXT = 1024 * 1024
MAX_PROVENANCE_TEXT = 128 * 1024


def _ts(name: str, *, nullable: bool = False) -> sa.Column[datetime]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "evidence",
        sa.Column("evidence_id", sa.Text(), primary_key=True),
        sa.Column("lineage_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "executive_id",
            sa.Text(),
            sa.ForeignKey("executives.executive_id"),
            nullable=False,
        ),
        sa.Column(
            "session_id",
            sa.Text(),
            sa.ForeignKey("sessions.session_id"),
            nullable=False,
        ),
        sa.Column("run_id", sa.Text(), sa.ForeignKey("runs.run_id"), nullable=False),
        sa.Column(
            "operation_id",
            sa.Text(),
            sa.ForeignKey("tool_executions.operation_id"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("subject_key", sa.Text(), nullable=False),
        sa.Column("authorization_version", sa.Integer(), nullable=False),
        sa.Column("scope_digest", sa.String(64), nullable=False),
        sa.Column("catalog_version", sa.Integer(), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("preference_fingerprint", sa.Text(), nullable=False),
        sa.Column("analysis", JSONB(), nullable=False),
        sa.Column("provenance", JSONB(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("grain", ARRAY(sa.Text()), nullable=False),
        sa.Column("analytical_slots", ARRAY(sa.Text()), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.Column("content_digest", sa.String(64), nullable=False),
        _ts("computed_at"),
        _ts("recorded_at"),
        sa.UniqueConstraint("operation_id", name="uq_evidence_operation"),
        sa.UniqueConstraint("lineage_id", "version", name="uq_evidence_lineage"),
        sa.CheckConstraint(
            "kind IN ('query', 'derived', 'external')", name="ck_evidence_kind"
        ),
        sa.CheckConstraint("version >= 1", name="ck_evidence_version"),
        sa.CheckConstraint(
            "char_length(subject_key) BETWEEN 1 AND 128", name="ck_evidence_subject"
        ),
        sa.CheckConstraint("authorization_version >= 0", name="ck_evidence_authz"),
        sa.CheckConstraint(
            f"octet_length(payload::text) <= {MAX_PAYLOAD_TEXT}",
            name="ck_evidence_payload_size",
        ),
        sa.CheckConstraint(
            f"octet_length(provenance::text) <= {MAX_PROVENANCE_TEXT}",
            name="ck_evidence_provenance_size",
        ),
    )
    op.create_index(
        "ix_evidence_lookup",
        "evidence",
        ["executive_id", "session_id", "subject_key", sa.text("computed_at DESC")],
    )
    op.create_index("ix_evidence_run", "evidence", ["run_id"])

    op.create_table(
        "evidence_dependencies",
        sa.Column(
            "evidence_id",
            sa.Text(),
            sa.ForeignKey("evidence.evidence_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("position", sa.Integer(), primary_key=True),
        sa.Column(
            "depends_on",
            sa.Text(),
            sa.ForeignKey("evidence.evidence_id"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_evidence_dependencies_input", "evidence_dependencies", ["depends_on"]
    )

    op.create_table(
        "run_evidence",
        sa.Column("run_id", sa.Text(), sa.ForeignKey("runs.run_id"), primary_key=True),
        sa.Column(
            "evidence_id",
            sa.Text(),
            sa.ForeignKey("evidence.evidence_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("use", sa.Text(), nullable=False),
        _ts("linked_at"),
        sa.CheckConstraint("use IN ('produced', 'reused')", name="ck_run_evidence_use"),
    )
    op.create_index("ix_run_evidence_evidence", "run_evidence", ["evidence_id"])

    op.create_table(
        "evidence_invalidations",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "evidence_id",
            sa.Text(),
            sa.ForeignKey("evidence.evidence_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("slot", sa.Text()),
        _ts("invalidated_at"),
        sa.UniqueConstraint("evidence_id", name="uq_evidence_invalidation"),
        sa.CheckConstraint(
            "reason IN ('preference_changed')", name="ck_evidence_invalidation_reason"
        ),
    )

    op.create_table(
        "evidence_pins",
        sa.Column(
            "evidence_id",
            sa.Text(),
            sa.ForeignKey("evidence.evidence_id"),
            primary_key=True,
        ),
        sa.Column("holder_kind", sa.Text(), primary_key=True),
        sa.Column("holder_id", sa.Text(), primary_key=True),
        _ts("pinned_at"),
        sa.CheckConstraint(
            "char_length(holder_kind) BETWEEN 1 AND 32", name="ck_evidence_pin_kind"
        ),
    )
    op.create_index(
        "ix_evidence_pins_holder", "evidence_pins", ["holder_kind", "holder_id"]
    )

    # reject_history_update() was created by revision 0002.
    for table in APPEND_ONLY:
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_history_update()"
        )


def downgrade() -> None:
    for table in APPEND_ONLY:
        op.execute(f"DROP TRIGGER {table}_append_only ON {table}")
    op.drop_table("evidence_pins")
    op.drop_table("evidence_invalidations")
    op.drop_table("run_evidence")
    op.drop_table("evidence_dependencies")
    op.drop_table("evidence")
