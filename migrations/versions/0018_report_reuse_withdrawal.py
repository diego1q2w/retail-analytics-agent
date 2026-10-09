"""Soft-deleting a report stops reuse through it (T18-F5).

- ``session_report_evidence`` keeps one link per report the evidence was
  reached through (primary key now includes ``report_id``), so a record linked
  through another, still valid report stays usable when one report goes.
- ``withdrawn_at``/``withdrawn_reason``: set in the deletion transaction
  (``report_deleted``). Restoring the report never revives a link by itself:
  it becomes ``revalidation_pending`` and only the re-validation (access,
  definitions, evidence validity) reinstates it or marks it
  ``revalidation_failed``. ``revalidated_at``/``revalidation_result`` record
  the latest re-validation (``reinstated``, ``reimported`` or a refusal code).
- Links of reports that were already soft-deleted are withdrawn here too.
- ``evidence_source_withdrawn(id)``: true when a record was derived (directly
  or through records of its own session) from report evidence of another
  session that no longer has a live link into that session.

Revision ID: 0018
Revises: 0017
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "session_report_evidence"
_PK = "session_report_evidence_pkey"

_SOURCE_WITHDRAWN = """
CREATE FUNCTION evidence_source_withdrawn(target text) RETURNS boolean
LANGUAGE sql STABLE AS $$
    WITH RECURSIVE home AS (
        SELECT session_id FROM evidence WHERE evidence_id = target
    ), inputs(evidence_id) AS (
        SELECT d.depends_on FROM evidence_dependencies d
        WHERE d.evidence_id = target
        UNION
        SELECT d.depends_on
        FROM inputs i
        JOIN evidence e ON e.evidence_id = i.evidence_id
        JOIN evidence_dependencies d ON d.evidence_id = i.evidence_id
        WHERE e.session_id = (SELECT session_id FROM home)
    )
    SELECT EXISTS (
        SELECT 1
        FROM inputs i
        JOIN evidence e ON e.evidence_id = i.evidence_id
        WHERE e.session_id <> (SELECT session_id FROM home)
          AND NOT EXISTS (
              SELECT 1 FROM session_report_evidence s
              WHERE s.session_id = (SELECT session_id FROM home)
                AND s.evidence_id = i.evidence_id
                AND s.withdrawn_at IS NULL
          )
    )
$$
"""


def upgrade() -> None:
    op.drop_constraint(_PK, _TABLE, type_="primary")
    op.create_primary_key(_PK, _TABLE, ["session_id", "evidence_id", "report_id"])
    op.add_column(_TABLE, sa.Column("withdrawn_at", sa.DateTime(timezone=True)))
    op.add_column(_TABLE, sa.Column("withdrawn_reason", sa.Text()))
    op.add_column(_TABLE, sa.Column("revalidated_at", sa.DateTime(timezone=True)))
    op.add_column(_TABLE, sa.Column("revalidation_result", sa.Text()))
    op.create_check_constraint(
        "ck_session_report_withdrawn",
        _TABLE,
        "(withdrawn_at IS NULL) = (withdrawn_reason IS NULL) AND "
        "(withdrawn_reason IS NULL OR withdrawn_reason IN "
        "('report_deleted', 'revalidation_pending', 'revalidation_failed'))",
    )
    op.create_index("ix_session_report_evidence_report", _TABLE, ["report_id"])
    op.execute(
        """
        UPDATE session_report_evidence s
        SET withdrawn_at = r.deleted_at, withdrawn_reason = 'report_deleted'
        FROM reports r
        WHERE r.report_id = s.report_id AND r.deleted_at IS NOT NULL
        """
    )
    op.execute(_SOURCE_WITHDRAWN)


def downgrade() -> None:
    op.execute("DROP FUNCTION evidence_source_withdrawn(text)")
    # The old schema cannot express a withdrawn link: drop them (fail closed).
    op.execute("DELETE FROM session_report_evidence WHERE withdrawn_at IS NOT NULL")
    op.drop_index("ix_session_report_evidence_report", table_name=_TABLE)
    op.drop_constraint("ck_session_report_withdrawn", _TABLE, type_="check")
    for column in (
        "revalidation_result",
        "revalidated_at",
        "withdrawn_reason",
        "withdrawn_at",
    ):
        op.drop_column(_TABLE, column)
    # Keep one link per (session, evidence): the newest import.
    op.execute(
        """
        DELETE FROM session_report_evidence s
        USING session_report_evidence t
        WHERE s.session_id = t.session_id AND s.evidence_id = t.evidence_id
          AND (s.imported_at, s.report_id) < (t.imported_at, t.report_id)
        """
    )
    op.drop_constraint(_PK, _TABLE, type_="primary")
    op.create_primary_key(_PK, _TABLE, ["session_id", "evidence_id"])
