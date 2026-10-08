"""Durable Golden Knowledge embeddings.

``golden_embeddings`` caches the vector of one piece of reviewed content for
one embedding model and dimension count. The key is the content digest plus
model id plus dimensions, so a different model or size gets its own rows and
never reuses an incompatible vector. The table holds no text, no product
identifiers and no access policy: access stays in the Golden tables and is
checked before anything is scored. Rows for content that no longer exists in
``golden_versions`` (erased) are deleted with the erasure.

Revision ID: 0009
Revises: 0008
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "golden_embeddings",
        sa.Column("content_digest", sa.String(64), primary_key=True),
        sa.Column("model_id", sa.Text(), primary_key=True),
        sa.Column("dimensions", sa.Integer(), primary_key=True),
        sa.Column("vector", ARRAY(sa.Float(53)), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("dimensions > 0", name="ck_golden_embeddings_dims"),
        sa.CheckConstraint(
            "cardinality(vector) = dimensions", name="ck_golden_embeddings_size"
        ),
    )


def downgrade() -> None:
    op.drop_table("golden_embeddings")
