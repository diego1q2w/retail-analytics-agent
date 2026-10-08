"""Cross-session preference memory with its proposals and audit trail.

- ``user_preferences``: one current value per (executive, slot) at the
  ``user_default`` scope and per (executive, session, slot) at ``session``
  scope, with source and a version that increases on every change. Values use
  a restricted character set and length, so free text cannot be stored.
  Session rows go with their session; default rows go with the executive's
  explicit forget requests.
- ``preference_proposals``: repeated behaviour seen in one session. Only an
  explicit confirmation before ``expires_at`` turns it into a default.
- ``preference_events``: append-only audit of remember/change/forget/propose/
  confirm/decline actions. It records slot, scope, source and version but never
  the value, so forgetting really forgets.

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

VALUE_RULE = "value ~ '^[A-Za-z0-9_@/.+-]{1,80}$'"
SLOT_RULE = "slot ~ '^[a-z_]{1,40}(:[a-z][a-z0-9_]{0,39})?$'"


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _timestamp(name: str, *, nullable: bool = False) -> sa.Column[sa.DateTime]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "user_preferences",
        sa.Column("preference_id", sa.Text(), primary_key=True),
        sa.Column(
            "executive_id",
            sa.Text(),
            sa.ForeignKey("executives.executive_id"),
            nullable=False,
        ),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column(
            "session_id",
            sa.Text(),
            sa.ForeignKey("sessions.session_id", ondelete="CASCADE"),
        ),
        sa.Column("slot", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.CheckConstraint(
            _in("scope", ("session", "user_default")), name="ck_pref_scope"
        ),
        sa.CheckConstraint(
            _in("source", ("explicit", "inferred_confirmed", "inferred_session")),
            name="ck_pref_source",
        ),
        sa.CheckConstraint(
            "(scope = 'session') = (session_id IS NOT NULL)", name="ck_pref_session"
        ),
        sa.CheckConstraint(
            "scope = 'session' OR source <> 'inferred_session'",
            name="ck_pref_inference_confirmed",
        ),
        sa.CheckConstraint("version >= 1", name="ck_pref_version"),
        sa.CheckConstraint(VALUE_RULE, name="ck_pref_value"),
        sa.CheckConstraint(SLOT_RULE, name="ck_pref_slot"),
    )
    op.create_index(
        "uq_pref_default",
        "user_preferences",
        ["executive_id", "slot"],
        unique=True,
        postgresql_where=sa.text("scope = 'user_default'"),
    )
    op.create_index(
        "uq_pref_session",
        "user_preferences",
        ["executive_id", "session_id", "slot"],
        unique=True,
        postgresql_where=sa.text("scope = 'session'"),
    )

    op.create_table(
        "preference_proposals",
        sa.Column("proposal_id", sa.Text(), primary_key=True),
        sa.Column(
            "executive_id",
            sa.Text(),
            sa.ForeignKey("executives.executive_id"),
            nullable=False,
        ),
        sa.Column(
            "session_id",
            sa.Text(),
            sa.ForeignKey("sessions.session_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("slot", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("observations", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        _timestamp("expires_at", nullable=True),
        sa.UniqueConstraint(
            "executive_id",
            "session_id",
            "slot",
            "value",
            name="uq_pref_proposal_observation",
        ),
        sa.CheckConstraint(
            _in("status", ("observing", "proposed", "confirmed", "declined")),
            name="ck_pref_proposal_status",
        ),
        sa.CheckConstraint("observations >= 0", name="ck_pref_proposal_count"),
        sa.CheckConstraint(VALUE_RULE, name="ck_pref_proposal_value"),
        sa.CheckConstraint(SLOT_RULE, name="ck_pref_proposal_slot"),
    )

    op.create_table(
        "preference_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("executive_id", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Text()),
        sa.Column("slot", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text()),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("source", sa.Text()),
        sa.Column("version", sa.Integer()),
        _timestamp("occurred_at"),
        sa.CheckConstraint(
            _in(
                "action",
                (
                    "remembered",
                    "changed",
                    "forgotten",
                    "adapted",
                    "proposed",
                    "confirmed",
                    "declined",
                ),
            ),
            name="ck_pref_event_action",
        ),
    )
    op.create_index(
        "ix_preference_events_executive", "preference_events", ["executive_id", "id"]
    )
    # reject_history_update() was created by revision 0002.
    op.execute(
        "CREATE TRIGGER preference_events_append_only BEFORE UPDATE ON "
        "preference_events FOR EACH ROW EXECUTE FUNCTION reject_history_update()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER preference_events_append_only ON preference_events")
    op.drop_table("preference_events")
    op.drop_table("preference_proposals")
    op.drop_table("user_preferences")
