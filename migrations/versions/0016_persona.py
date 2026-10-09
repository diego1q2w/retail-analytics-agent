"""Company persona versions, the active pointer, history and per-run pins.

- ``persona_versions``: free-text presentation defaults. A draft can be edited
  by its author (``revision`` counts edits); once published or discarded a
  version never changes (a trigger rejects it) and its text never changes in
  the transition itself, so history is exactly what runs saw.
- ``persona_state``: one row, the active version. Publication and rollback lock
  it, which serializes concurrent changes; the ``(expected_current)`` check in
  the application makes a lost update impossible.
- ``persona_publications``: append-only record of every change of the active
  version (publish or rollback).
- ``run_personas``: the version one run pinned when it started (null = none
  existed). Immutable, so a later publication never reaches a running run.

Revision ID: 0016
Revises: 0015
"""

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _ts(name: str, *, nullable: bool = False) -> sa.Column[datetime]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "persona_versions",
        sa.Column("version_id", sa.Text(), primary_key=True),
        sa.Column("number", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_digest", sa.String(64), nullable=False),
        sa.Column(
            "base_version_id",
            sa.Text(),
            sa.ForeignKey("persona_versions.version_id"),
        ),
        sa.Column(
            "author_id",
            sa.Text(),
            sa.ForeignKey("executives.executive_id"),
            nullable=False,
        ),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("findings", postgresql.JSONB(), nullable=False),
        sa.Column("previewed_digest", sa.String(64)),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
        _ts("first_published_at", nullable=True),
        sa.UniqueConstraint("number", name="uq_persona_versions_number"),
        sa.UniqueConstraint(
            "author_id", "idempotency_key", name="uq_persona_versions_idempotency"
        ),
        sa.CheckConstraint(
            "state IN ('draft', 'published', 'discarded')",
            name="ck_persona_versions_state",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_persona_versions_revision"),
        sa.CheckConstraint(
            "char_length(content) BETWEEN 1 AND 4000", name="ck_persona_versions_length"
        ),
        sa.CheckConstraint(
            "(state = 'published') = (first_published_at IS NOT NULL)"
            " OR state = 'discarded'",
            name="ck_persona_versions_published",
        ),
    )
    op.create_table(
        "persona_state",
        sa.Column("persona_id", sa.Text(), primary_key=True),
        sa.Column(
            "current_version_id",
            sa.Text(),
            sa.ForeignKey("persona_versions.version_id"),
        ),
        sa.Column("publication_seq", sa.BigInteger(), nullable=False),
    )
    op.execute(
        "INSERT INTO persona_state (persona_id, current_version_id, publication_seq)"
        " VALUES ('company', NULL, 0)"
    )
    op.create_table(
        "persona_publications",
        sa.Column("sequence", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "version_id",
            sa.Text(),
            sa.ForeignKey("persona_versions.version_id"),
            nullable=False,
        ),
        sa.Column(
            "previous_version_id",
            sa.Text(),
            sa.ForeignKey("persona_versions.version_id"),
        ),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        _ts("published_at"),
        sa.CheckConstraint(
            "action IN ('publish', 'rollback')", name="ck_persona_publications_action"
        ),
    )
    op.create_table(
        "run_personas",
        sa.Column("run_id", sa.Text(), sa.ForeignKey("runs.run_id"), primary_key=True),
        sa.Column(
            "version_id", sa.Text(), sa.ForeignKey("persona_versions.version_id")
        ),
        _ts("pinned_at"),
    )
    # reject_history_update() was created by revision 0002.
    for table in ("persona_publications", "run_personas"):
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_history_update()"
        )
    op.execute(
        """
        CREATE FUNCTION persona_versions_frozen() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.state <> 'draft' THEN
                RAISE EXCEPTION 'persona version % is frozen', OLD.version_id
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF NEW.state <> 'draft' AND (
                NEW.content IS DISTINCT FROM OLD.content
                OR NEW.content_digest IS DISTINCT FROM OLD.content_digest
                OR NEW.base_version_id IS DISTINCT FROM OLD.base_version_id
            ) THEN
                RAISE EXCEPTION 'persona text cannot change when freezing'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF NEW.version_id IS DISTINCT FROM OLD.version_id
               OR NEW.number IS DISTINCT FROM OLD.number
               OR NEW.author_id IS DISTINCT FROM OLD.author_id THEN
                RAISE EXCEPTION 'persona identity cannot change'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END $$;
        """
    )
    op.execute(
        "CREATE TRIGGER persona_versions_frozen BEFORE UPDATE ON persona_versions "
        "FOR EACH ROW EXECUTE FUNCTION persona_versions_frozen()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER persona_versions_frozen ON persona_versions")
    op.execute("DROP FUNCTION persona_versions_frozen()")
    for table in ("persona_publications", "run_personas"):
        op.execute(f"DROP TRIGGER {table}_append_only ON {table}")
    op.drop_table("run_personas")
    op.drop_table("persona_publications")
    op.drop_table("persona_state")
    op.drop_table("persona_versions")
