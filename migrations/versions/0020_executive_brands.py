"""Brand-based manager access (T05-F2).

- ``executive_brands``: the brands assigned to an executive (a manager owns
  one or more brands). Names are the catalog's exact spelling. Assigning or
  removing a brand bumps the executive's ``authorization_version`` and is
  audited (``access.brands_changed``).
- ``catalog_product_brands``: the trusted snapshot of ``products.brand``,
  replaced by an explicit catalog sync. Products without a usable brand are
  not stored, so no assignment can grant them. A sync that changes what an
  executive's brands resolve to bumps that executive's version.

An executive's products are ``product_entitlements`` (explicit grants) plus
the snapshot products of their assigned brands. Existing rows are unchanged:
explicit grants keep working as before.

Revision ID: 0020
Revises: 0019
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Matches domain.access.is_valid_brand (non-blank, no surrounding spaces).
_BRAND_CHECK = "char_length(brand) BETWEEN 1 AND 200 AND brand = btrim(brand)"


def _timestamp(name: str) -> sa.Column[sa.DateTime]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False)


def upgrade() -> None:
    op.create_table(
        "executive_brands",
        sa.Column(
            "executive_id",
            sa.Text(),
            sa.ForeignKey("executives.executive_id"),
            primary_key=True,
        ),
        sa.Column("brand", sa.Text(), primary_key=True),
        _timestamp("assigned_at"),
        sa.CheckConstraint(_BRAND_CHECK, name="ck_executive_brands_brand"),
    )
    op.create_index("ix_executive_brands_brand", "executive_brands", ["brand"])
    op.create_table(
        "catalog_product_brands",
        sa.Column("product_id", sa.Text(), primary_key=True),
        sa.Column("brand", sa.Text(), nullable=False),
        _timestamp("synced_at"),
        sa.CheckConstraint(
            "product_id ~ '^[1-9][0-9]{0,18}$'",
            name="ck_catalog_product_brands_id",
        ),
        sa.CheckConstraint(_BRAND_CHECK, name="ck_catalog_product_brands_brand"),
    )
    op.create_index(
        "ix_catalog_product_brands_brand", "catalog_product_brands", ["brand"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_catalog_product_brands_brand", table_name="catalog_product_brands"
    )
    op.drop_table("catalog_product_brands")
    op.drop_index("ix_executive_brands_brand", table_name="executive_brands")
    op.drop_table("executive_brands")
