"""Executives, their roles and server-side product entitlements.

- ``executives``: one row per authenticated identity (issuer + subject), with
  server-assigned roles, an active flag and ``authorization_version``, which
  every change to roles, products or status increments.
- ``product_entitlements``: the complete set of products whose data an
  executive may see. No rows means no product data, never unrestricted.

Sessions keep their plain ``executive_id`` (no foreign key) so existing
records and tests stay valid; ownership is enforced by the application.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLES = ("executive", "editor", "reviewer", "admin")


def _timestamp(name: str) -> sa.Column[sa.DateTime]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False)


def upgrade() -> None:
    roles = ", ".join(repr(role) for role in ROLES)
    op.create_table(
        "executives",
        sa.Column("executive_id", sa.Text(), primary_key=True),
        sa.Column("issuer", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("label", sa.String(120), nullable=False),
        sa.Column("roles", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("authorization_version", sa.Integer(), nullable=False),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.UniqueConstraint("issuer", "subject", name="uq_executives_identity"),
        sa.CheckConstraint(
            f"roles <@ ARRAY[{roles}]::text[]", name="ck_executives_roles"
        ),
        sa.CheckConstraint(
            "authorization_version >= 0", name="ck_executives_authorization_version"
        ),
    )
    op.create_table(
        "product_entitlements",
        sa.Column(
            "executive_id",
            sa.Text(),
            sa.ForeignKey("executives.executive_id"),
            primary_key=True,
        ),
        sa.Column("product_id", sa.Text(), primary_key=True),
        _timestamp("granted_at"),
        sa.CheckConstraint(
            "product_id ~ '^[1-9][0-9]{0,18}$'", name="ck_product_entitlements_id"
        ),
    )


def downgrade() -> None:
    op.drop_table("product_entitlements")
    op.drop_table("executives")
