"""Report access by required-scope coverage (T18-F1).

- ``product_scope_snapshots``: the exact product set behind an evidence
  authority stamp, keyed by its digest (SHA-256 of the sorted IDs joined by
  newlines). Written by trusted code with the evidence record; immutable; a
  CHECK ties the digest to the stored IDs, so a digest names exactly one set.
- ``report_required_scopes``: for a report version, the digest of the union of
  the sets its cited evidence was computed under. A version is readable while
  the owner's current products are a superset of it. Versions without a row
  keep the strict equal-digest rule.

Backfill (conservative): an existing evidence stamp is recovered only when it
equals the digest of some executive's *current* entitlements (trusted data;
the digest proves the set is identical). A report version gets a required
scope only when every cited evidence stamp was recovered; otherwise it stays
on the strict rule.

Revision ID: 0015
Revises: 0014
"""

import hashlib
from collections.abc import Iterable, Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY = ("product_scope_snapshots", "report_required_scopes")

_snapshots = sa.table(
    "product_scope_snapshots",
    sa.column("scope_digest", sa.String),
    sa.column("product_ids", postgresql.ARRAY(sa.Text)),
    sa.column("recorded_at", sa.DateTime(timezone=True)),
)
_required = sa.table(
    "report_required_scopes",
    sa.column("report_id", sa.Text),
    sa.column("version", sa.Integer),
    sa.column("scope_digest", sa.String),
)


def upgrade() -> None:
    op.create_table(
        "product_scope_snapshots",
        sa.Column("scope_digest", sa.String(64), primary_key=True),
        sa.Column("product_ids", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "cardinality(product_ids) > 0", name="ck_product_scope_not_empty"
        ),
        sa.CheckConstraint(
            "scope_digest = encode(sha256(convert_to("
            "array_to_string(product_ids, chr(10)), 'UTF8')), 'hex')",
            name="ck_product_scope_digest",
        ),
    )
    op.create_table(
        "report_required_scopes",
        sa.Column("report_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "scope_digest",
            sa.String(64),
            sa.ForeignKey("product_scope_snapshots.scope_digest"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("report_id", "version"),
        sa.ForeignKeyConstraint(
            ["report_id", "version"],
            ["report_versions.report_id", "report_versions.version"],
            ondelete="CASCADE",
        ),
    )
    # reject_history_update() was created by revision 0002.
    for table in APPEND_ONLY:
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_history_update()"
        )
    # Offline (--sql) output has no data to read: every existing version then
    # simply keeps the strict rule, which is the safe default.
    if not op.get_context().as_sql:
        _backfill(op.get_bind())


def downgrade() -> None:
    for table in APPEND_ONLY:
        op.execute(f"DROP TRIGGER {table}_append_only ON {table}")
    op.drop_table("report_required_scopes")
    op.drop_table("product_scope_snapshots")


def _digest(ids: Iterable[str]) -> str:
    return hashlib.sha256("\n".join(sorted(set(ids))).encode()).hexdigest()


def _store(connection: sa.Connection, ids: Iterable[str]) -> str:
    ordered = sorted(set(ids))
    digest = _digest(ordered)
    connection.execute(
        postgresql.insert(_snapshots)
        .values(scope_digest=digest, product_ids=ordered, recorded_at=sa.func.now())
        .on_conflict_do_nothing(index_elements=["scope_digest"])
    )
    return digest


def _backfill(connection: sa.Connection) -> None:
    known: dict[str, frozenset[str]] = {}
    current = connection.execute(
        sa.text(
            "SELECT executive_id, array_agg(product_id) AS ids "
            "FROM product_entitlements GROUP BY executive_id"
        )
    ).all()
    for _executive_id, ids in current:
        digest = _digest(ids)
        stamped = connection.execute(
            sa.text("SELECT EXISTS (SELECT 1 FROM evidence WHERE scope_digest = :d)"),
            {"d": digest},
        ).scalar_one()
        if stamped:
            known[_store(connection, ids)] = frozenset(ids)
    if not known:
        return
    cited = connection.execute(
        sa.text(
            "SELECT re.report_id, re.version, "
            "array_agg(DISTINCT e.scope_digest) AS digests "
            "FROM report_evidence re JOIN evidence e USING (evidence_id) "
            "GROUP BY re.report_id, re.version"
        )
    ).all()
    for report_id, version, digests in cited:
        if not digests or any(d not in known for d in digests):
            continue
        union: set[str] = set()
        for d in digests:
            union |= known[d]
        connection.execute(
            sa.insert(_required).values(
                report_id=report_id,
                version=version,
                scope_digest=_store(connection, union),
            )
        )
