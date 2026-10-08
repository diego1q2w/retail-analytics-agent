"""Golden Knowledge: reviewed, independently versioned analytical examples.

- ``golden_versions``: one row per example version with status, access
  metadata (shared vs restricted products), schema/metric applicability and
  the sanitized trio. Content columns are NULL exactly when the version is
  ``erased``; the report body lives in the artifact store. At most one
  version per example is ``published``.
- ``golden_provenance``: opaque source references only (no content), so a
  privacy removal or an incorrect-source finding can locate derived versions
  after the source report is gone.
- ``golden_review_events`` / ``golden_index_events``: append-only audit of
  each status change and the ordered feed of index invalidations. Neither
  holds example content.

Revision ID: 0006
Revises: 0005
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES = "('candidate','published','suspended','rejected','retired','erased')"
ACTIONS = (
    "('submit','approve','reject','suspend','reinstate','retire','supersede','erase')"
)


def upgrade() -> None:
    op.create_table(
        "golden_versions",
        sa.Column("example_id", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("author_id", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column(
            "restricted_product_ids",
            ARRAY(sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("metric_refs", JSONB(), nullable=False),
        sa.Column("question", sa.Text()),
        sa.Column("sql_text", sa.Text()),
        sa.Column("method_summary", sa.Text()),
        sa.Column("report_artifact_id", sa.Text()),
        sa.Column("report_artifact_version", sa.Integer()),
        sa.Column("content_digest", sa.String(64)),
        # Report artifact still to be purged after an erasure.
        sa.Column("purge_artifact_id", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_by", sa.Text()),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("example_id", "version"),
        sa.UniqueConstraint(
            "author_id", "idempotency_key", name="uq_golden_versions_idempotency"
        ),
        sa.CheckConstraint(f"status IN {STATUSES}", name="ck_golden_versions_status"),
        sa.CheckConstraint("version >= 1", name="ck_golden_versions_version"),
        sa.CheckConstraint(
            "(status = 'erased' AND question IS NULL AND sql_text IS NULL "
            "AND method_summary IS NULL AND report_artifact_id IS NULL "
            "AND report_artifact_version IS NULL AND content_digest IS NULL) "
            "OR (status <> 'erased' AND question IS NOT NULL "
            "AND sql_text IS NOT NULL AND method_summary IS NOT NULL "
            "AND report_artifact_id IS NOT NULL "
            "AND report_artifact_version IS NOT NULL AND content_digest IS NOT NULL)",
            name="ck_golden_versions_erasure",
        ),
        sa.CheckConstraint(
            "purge_artifact_id IS NULL OR status = 'erased'",
            name="ck_golden_versions_purge",
        ),
    )
    op.create_index(
        "uq_golden_versions_one_published",
        "golden_versions",
        ["example_id"],
        unique=True,
        postgresql_where=sa.text("status = 'published'"),
    )
    op.create_index("ix_golden_versions_status", "golden_versions", ["status"])
    op.create_index(
        "ix_golden_versions_purge",
        "golden_versions",
        ["example_id", "version"],
        postgresql_where=sa.text("purge_artifact_id IS NOT NULL"),
    )

    op.create_table(
        "golden_provenance",
        sa.Column("example_id", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("source_kind", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Text()),
        sa.Column("source_version", sa.Integer()),
        sa.PrimaryKeyConstraint("example_id", "version"),
        sa.ForeignKeyConstraint(
            ["example_id", "version"],
            ["golden_versions.example_id", "golden_versions.version"],
        ),
        sa.CheckConstraint(
            "source_kind IN ('authored','report','investigation') "
            "AND ((source_kind = 'authored') = (source_id IS NULL))",
            name="ck_golden_provenance_source",
        ),
    )
    op.create_index(
        "ix_golden_provenance_source", "golden_provenance", ["source_kind", "source_id"]
    )

    op.create_table(
        "golden_review_events",
        sa.Column("event_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("example_id", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("from_status", sa.Text()),
        sa.Column("to_status", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("checks", JSONB()),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(f"action IN {ACTIONS}", name="ck_golden_review_action"),
    )
    op.create_index(
        "ix_golden_review_events_version",
        "golden_review_events",
        ["example_id", "version", "event_id"],
    )

    op.create_table(
        "golden_index_events",
        sa.Column("sequence", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("example_id", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('upsert','remove')", name="ck_golden_index_kind"),
    )
    for table in ("golden_review_events", "golden_index_events"):
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_history_update()"
        )


def downgrade() -> None:
    op.drop_table("golden_index_events")
    op.drop_table("golden_review_events")
    op.drop_table("golden_provenance")
    op.drop_table("golden_versions")
