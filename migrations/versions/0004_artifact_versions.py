"""Artifact version metadata (content lives outside PostgreSQL).

- ``artifact_versions``: one immutable row per stored version with owner,
  media type, checksum, size and the server-derived storage key. Rows cannot
  be updated (trigger from 0002); deletion is for retention purges only.
- ``(owner_id, idempotency_key)`` is unique, so a retried save returns the
  original version.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "artifact_versions",
        sa.Column("artifact_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.Text(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("artifact_id", "version"),
        sa.UniqueConstraint(
            "owner_id", "idempotency_key", name="uq_artifact_versions_idempotency"
        ),
        sa.CheckConstraint("version >= 1", name="ck_artifact_versions_version"),
        sa.CheckConstraint("size_bytes > 0", name="ck_artifact_versions_size"),
        sa.CheckConstraint(
            "artifact_id ~ '^[0-9a-f]{32}$' AND sha256 ~ '^[0-9a-f]{64}$' "
            "AND storage_key = artifact_id || '/' || sha256",
            name="ck_artifact_versions_key",
        ),
    )
    op.create_index(
        "ix_artifact_versions_storage_key", "artifact_versions", ["storage_key"]
    )
    op.execute(
        "CREATE TRIGGER artifact_versions_append_only "
        "BEFORE UPDATE ON artifact_versions "
        "FOR EACH ROW EXECUTE FUNCTION reject_history_update()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER artifact_versions_append_only ON artifact_versions")
    op.drop_table("artifact_versions")
