"""PostgreSQL evidence store: records, run links, invalidations and pins.

Evidence rows are append-only (an UPDATE trigger rejects changes); refreshes
insert the next version of a lineage. Each public method is one transaction.
The store checks ownership linkage it can see (refresh and pin owners) but
reuse decisions belong to ``EvidenceService`` and the domain policy.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import exc
from sqlalchemy.dialects.postgresql import aggregate_order_by, distinct_on
from sqlalchemy.dialects.postgresql import insert as pg_insert

from retail_analytics.adapters.postgres.audit import append_audit
from retail_analytics.adapters.postgres.database import Database, violated_constraint
from retail_analytics.adapters.postgres.product_scopes import record_snapshot
from retail_analytics.adapters.postgres.schema import (
    evidence,
    evidence_dependencies,
    evidence_invalidations,
    evidence_pins,
    reports,
    run_evidence,
    session_report_evidence,
    sessions,
)
from retail_analytics.application.authorization import AccessDenied
from retail_analytics.application.contracts.audit import AuditEvent
from retail_analytics.application.contracts.evidence import (
    DEFAULT_CANDIDATE_LIMIT,
    REVALIDATION_REIMPORTED,
    REVALIDATION_REINSTATED,
    WITHDRAWN_REVALIDATION_FAILED,
    WITHDRAWN_REVALIDATION_PENDING,
    ImportedEvidence,
    NewEvidence,
    NewEvidenceImport,
    PendingReuseLink,
    ReuseLinkVerdict,
    ReuseRevalidation,
    RunEvidenceLink,
    StoredEvidence,
)
from retail_analytics.application.contracts.persistence import IdempotencyConflict
from retail_analytics.domain.evidence import (
    AuthorityStamp,
    Evidence,
    EvidenceContent,
    EvidenceError,
    EvidenceKind,
    EvidenceUse,
    PinHolder,
    ReportSource,
    decode_analysis,
    decode_provenance,
    decode_table,
    encode_analysis,
    encode_provenance,
    encode_table,
)
from retail_analytics.domain.lifecycle import REUSE_REVALIDATED

type Row = sa.Row[Any]

_INVALIDATE = sa.text(
    """
    WITH RECURSIVE affected(evidence_id) AS (
        SELECT e.evidence_id FROM evidence e
        WHERE e.executive_id = :executive_id
          AND (CAST(:session_id AS text) IS NULL OR e.session_id = :session_id)
          AND :slot = ANY(e.analytical_slots)
        UNION
        SELECT d.evidence_id FROM evidence_dependencies d
        JOIN affected a ON d.depends_on = a.evidence_id
    )
    INSERT INTO evidence_invalidations (evidence_id, reason, slot, invalidated_at)
    SELECT evidence_id, 'preference_changed', :slot, :now FROM affected
    ON CONFLICT (evidence_id) DO NOTHING
    """
)

# A setting scoped to one session also supersedes report evidence linked into
# that session (the record itself stays valid for its own session).
_INVALIDATE_IMPORTS = sa.text(
    """
    UPDATE session_report_evidence s SET invalidated_at = :now
    FROM evidence e
    WHERE s.session_id = :session_id
      AND s.executive_id = :executive_id
      AND s.invalidated_at IS NULL
      AND e.evidence_id = s.evidence_id
      AND :slot = ANY(e.analytical_slots)
    """
)


def _select() -> sa.Select[Any]:
    deps = (
        sa.select(
            sa.func.coalesce(
                sa.func.array_agg(
                    aggregate_order_by(
                        evidence_dependencies.c.depends_on,
                        evidence_dependencies.c.position,
                    )
                ),
                sa.literal([], type_=sa.ARRAY(sa.Text)),
            )
        )
        .where(evidence_dependencies.c.evidence_id == evidence.c.evidence_id)
        .scalar_subquery()
        .label("derived_from")
    )
    invalidated = (
        sa.exists()
        .where(evidence_invalidations.c.evidence_id == evidence.c.evidence_id)
        .label("invalidated")
    )
    # Derived from report evidence whose links into this record's session
    # were all withdrawn (SQL function from migration 0018).
    source_withdrawn = sa.func.evidence_source_withdrawn(
        evidence.c.evidence_id, type_=sa.Boolean
    ).label("source_withdrawn")
    return sa.select(evidence, deps, invalidated, source_withdrawn)


def _stored(row: Row) -> StoredEvidence:
    m = row._mapping
    content = EvidenceContent(
        kind=EvidenceKind(m["kind"]),
        subject_key=m["subject_key"],
        analysis=decode_analysis(m["analysis"]),
        provenance=decode_provenance(m["provenance"]),
        table=decode_table(m["payload"]),
        grain=tuple(m["grain"]),
        analytical_slots=frozenset(m["analytical_slots"]),
        derived_from=tuple(m["derived_from"] or ()),
    )
    record = Evidence(
        evidence_id=m["evidence_id"],
        lineage_id=m["lineage_id"],
        version=m["version"],
        executive_id=m["executive_id"],
        session_id=m["session_id"],
        run_id=m["run_id"],
        operation_id=m["operation_id"],
        authority=AuthorityStamp(m["authorization_version"], m["scope_digest"]),
        content=content,
        computed_at=m["computed_at"],
        content_digest=m["content_digest"],
    )
    return StoredEvidence(record, bool(m["invalidated"]), bool(m["source_withdrawn"]))


class PostgresEvidenceStore:
    """Implements ``EvidenceRepository``, ``SessionEvidenceImports``,
    ``ReuseLinkRevalidation``, ``EvidencePins`` and the preference
    ``FindingInvalidator`` port."""

    def __init__(self, db: Database) -> None:
        self._db = db

    # --- EvidenceRepository -------------------------------------------------

    async def record(self, new: NewEvidence) -> Evidence:
        try:
            return await self._db.transaction(self._record, new)
        except exc.IntegrityError as error:
            if violated_constraint(error) != "uq_evidence_operation":
                raise
        # A concurrent attempt of the same operation won the insert.
        return await self._db.transaction(self._existing, new)

    def _by_operation(
        self, connection: sa.Connection, operation_id: str
    ) -> StoredEvidence | None:
        row = connection.execute(
            _select().where(evidence.c.operation_id == operation_id)
        ).one_or_none()
        return None if row is None else _stored(row)

    def _existing(self, connection: sa.Connection, new: NewEvidence) -> Evidence:
        found = self._by_operation(connection, new.operation_id)
        if found is None:
            raise IdempotencyConflict("evidence", new.operation_id)
        if found.evidence.content_digest != new.content_digest or (
            found.evidence.executive_id != new.executive_id
        ):
            raise IdempotencyConflict("evidence", new.operation_id)
        return found.evidence

    def _record(self, connection: sa.Connection, new: NewEvidence) -> Evidence:
        if self._by_operation(connection, new.operation_id) is not None:
            return self._existing(connection, new)
        lineage_id, version = new.evidence_id, 1
        if new.refreshes is not None:
            previous = connection.execute(
                sa.select(
                    evidence.c.lineage_id,
                    evidence.c.executive_id,
                    evidence.c.session_id,
                ).where(evidence.c.evidence_id == new.refreshes)
            ).one_or_none()
            if previous is None or (previous.executive_id, previous.session_id) != (
                new.executive_id,
                new.session_id,
            ):
                raise AccessDenied("evidence", new.refreshes)
            lineage_id = previous.lineage_id
            versions = connection.execute(
                sa.select(evidence.c.version)
                .where(evidence.c.lineage_id == lineage_id)
                .with_for_update()
            ).scalars()
            version = max(versions, default=0) + 1
        content = new.content
        now = self._db.clock()
        # The exact set behind the authority stamp, kept apart from the record
        # for report access checks (never returned with the evidence).
        if record_snapshot(connection, new.scope_products, now) != (
            new.authority.scope_digest
        ):
            raise EvidenceError("scope products do not match the authority stamp")
        connection.execute(
            sa.insert(evidence).values(
                evidence_id=new.evidence_id,
                lineage_id=lineage_id,
                version=version,
                executive_id=new.executive_id,
                session_id=new.session_id,
                run_id=new.run_id,
                operation_id=new.operation_id,
                kind=content.kind.value,
                subject_key=content.subject_key,
                authorization_version=new.authority.authorization_version,
                scope_digest=new.authority.scope_digest,
                catalog_version=content.analysis.catalog_version,
                policy_version=content.analysis.policy_version,
                preference_fingerprint=content.analysis.preference_fingerprint,
                analysis=encode_analysis(content.analysis),
                provenance=encode_provenance(content.provenance),
                payload=encode_table(content.table),
                grain=list(content.grain),
                analytical_slots=sorted(content.analytical_slots),
                truncated=content.table.truncated,
                content_digest=new.content_digest,
                computed_at=new.computed_at,
                recorded_at=now,
            )
        )
        if content.derived_from:
            owned = connection.execute(
                sa.select(sa.func.count()).where(
                    evidence.c.evidence_id.in_(content.derived_from),
                    evidence.c.executive_id == new.executive_id,
                )
            ).scalar_one()
            if owned != len(set(content.derived_from)):
                raise AccessDenied("evidence", new.evidence_id)
            connection.execute(
                sa.insert(evidence_dependencies),
                [
                    {
                        "evidence_id": new.evidence_id,
                        "position": i,
                        "depends_on": input_id,
                    }
                    for i, input_id in enumerate(content.derived_from)
                ],
            )
        self._link(connection, new.run_id, new.evidence_id, EvidenceUse.PRODUCED)
        row = connection.execute(
            _select().where(evidence.c.evidence_id == new.evidence_id)
        ).one()
        return _stored(row).evidence

    async def get(self, evidence_id: str) -> StoredEvidence | None:
        return await self._db.transaction(self._get, evidence_id)

    @staticmethod
    def _get(connection: sa.Connection, evidence_id: str) -> StoredEvidence | None:
        row = connection.execute(
            _select().where(evidence.c.evidence_id == evidence_id)
        ).one_or_none()
        return None if row is None else _stored(row)

    async def candidates(
        self,
        executive_id: str,
        session_id: str,
        *,
        subject_key: str | None = None,
        limit: int = DEFAULT_CANDIDATE_LIMIT,
    ) -> Sequence[StoredEvidence]:
        query = _select().where(
            evidence.c.executive_id == executive_id,
            evidence.c.session_id == session_id,
        )
        if subject_key is not None:
            query = query.where(evidence.c.subject_key == subject_key)
        query = query.order_by(
            evidence.c.computed_at.desc(), evidence.c.version.desc()
        ).limit(limit)

        def work(connection: sa.Connection) -> list[StoredEvidence]:
            return [_stored(row) for row in connection.execute(query)]

        return await self._db.transaction(work)

    async def link_run(self, run_id: str, evidence_id: str, use: EvidenceUse) -> None:
        await self._db.transaction(self._link, run_id, evidence_id, use)

    def _link(
        self,
        connection: sa.Connection,
        run_id: str,
        evidence_id: str,
        use: EvidenceUse,
    ) -> None:
        connection.execute(
            pg_insert(run_evidence)
            .values(
                run_id=run_id,
                evidence_id=evidence_id,
                use=use.value,
                linked_at=self._db.clock(),
            )
            .on_conflict_do_nothing()
        )

    async def for_run(self, run_id: str) -> Sequence[RunEvidenceLink]:
        def work(connection: sa.Connection) -> list[RunEvidenceLink]:
            rows = connection.execute(
                sa.select(run_evidence)
                .where(run_evidence.c.run_id == run_id)
                .order_by(run_evidence.c.linked_at, run_evidence.c.evidence_id)
            )
            return [
                RunEvidenceLink(
                    r.run_id, r.evidence_id, EvidenceUse(r.use), r.linked_at
                )
                for r in rows
            ]

        return await self._db.transaction(work)

    # --- SessionEvidenceImports -------------------------------------------

    async def add_import(self, new: NewEvidenceImport) -> bool:
        def work(connection: sa.Connection) -> bool:
            owner = connection.execute(
                sa.select(evidence.c.executive_id).where(
                    evidence.c.evidence_id == new.evidence_id
                )
            ).scalar_one_or_none()
            session_owner = connection.execute(
                sa.select(sessions.c.executive_id).where(
                    sessions.c.session_id == new.session_id
                )
            ).scalar_one_or_none()
            if owner != new.executive_id or session_owner != new.executive_id:
                raise AccessDenied("evidence", new.evidence_id)
            # Shares the row lock the deletion confirmation takes for update,
            # so a deletion either precedes this check or waits for the link
            # (and then withdraws it).
            report = connection.execute(
                sa.select(reports.c.owner_id, reports.c.deleted_at)
                .where(reports.c.report_id == new.report_id)
                .with_for_update(read=True)
            ).one_or_none()
            if report is None or report.owner_id != new.executive_id:
                raise AccessDenied("report", new.report_id)
            if report.deleted_at is not None:
                return False
            now = self._db.clock()
            insert = pg_insert(session_report_evidence).values(
                session_id=new.session_id,
                evidence_id=new.evidence_id,
                executive_id=new.executive_id,
                run_id=new.run_id,
                report_id=new.report_id,
                report_version=new.report_version,
                report_title=new.report_title,
                imported_at=now,
            )
            # The caller has just run every reuse check on a live report: a
            # link withdrawn earlier comes back as a recorded re-validation.
            connection.execute(
                insert.on_conflict_do_update(
                    index_elements=["session_id", "evidence_id", "report_id"],
                    set_={
                        "withdrawn_at": None,
                        "withdrawn_reason": None,
                        "revalidated_at": now,
                        "revalidation_result": REVALIDATION_REIMPORTED,
                        "report_version": insert.excluded.report_version,
                        "report_title": insert.excluded.report_title,
                        "run_id": insert.excluded.run_id,
                        "imported_at": now,
                    },
                    where=session_report_evidence.c.withdrawn_at.is_not(None),
                )
            )
            self._link(connection, new.run_id, new.evidence_id, EvidenceUse.REUSED)
            return True

        return await self._db.transaction(work)

    async def imported(
        self,
        executive_id: str,
        session_id: str,
        *,
        evidence_id: str | None = None,
        subject_key: str | None = None,
        limit: int = DEFAULT_CANDIDATE_LIMIT,
    ) -> Sequence[ImportedEvidence]:
        imports = session_report_evidence
        # One link per record: a live one if any (not superseded first), else
        # the newest withdrawn one.
        link_query = sa.select(imports).where(
            imports.c.session_id == session_id,
            imports.c.executive_id == executive_id,
        )
        if evidence_id is not None:
            link_query = link_query.where(imports.c.evidence_id == evidence_id)
        chosen = (
            link_query.ext(distinct_on(imports.c.evidence_id))
            .order_by(
                imports.c.evidence_id,
                imports.c.withdrawn_at.is_not(None),
                imports.c.invalidated_at.is_not(None),
                imports.c.imported_at.desc(),
                imports.c.report_id,
            )
            .subquery("chosen")
        )
        query = (
            _select()
            .add_columns(
                chosen.c.report_id,
                chosen.c.report_version,
                chosen.c.report_title,
                chosen.c.imported_at,
                chosen.c.invalidated_at,
                chosen.c.withdrawn_at,
            )
            .join(chosen, chosen.c.evidence_id == evidence.c.evidence_id)
            .where(evidence.c.executive_id == executive_id)
        )
        if subject_key is not None:
            query = query.where(evidence.c.subject_key == subject_key)
        query = query.order_by(
            chosen.c.imported_at.desc(), evidence.c.evidence_id
        ).limit(limit)

        def work(connection: sa.Connection) -> list[ImportedEvidence]:
            found: list[ImportedEvidence] = []
            for row in connection.execute(query):
                stored = _stored(row)
                m = row._mapping
                found.append(
                    ImportedEvidence(
                        stored.evidence,
                        ReportSource(
                            m["report_id"],
                            m["report_version"],
                            m["report_title"],
                            m["imported_at"],
                        ),
                        stored.invalidated or m["invalidated_at"] is not None,
                        withdrawn=m["withdrawn_at"] is not None,
                    )
                )
            return found

        return await self._db.transaction(work)

    # --- ReuseLinkRevalidation --------------------------------------------

    async def pending_links(
        self, owner_id: str, report_id: str
    ) -> Sequence[PendingReuseLink]:
        imports = session_report_evidence

        def work(connection: sa.Connection) -> list[PendingReuseLink]:
            rows = connection.execute(
                sa.select(
                    imports.c.session_id,
                    imports.c.evidence_id,
                    imports.c.report_version,
                    imports.c.invalidated_at,
                )
                .where(
                    imports.c.report_id == report_id,
                    imports.c.executive_id == owner_id,
                    imports.c.withdrawn_reason == WITHDRAWN_REVALIDATION_PENDING,
                )
                .order_by(imports.c.session_id, imports.c.evidence_id)
            )
            return [
                PendingReuseLink(
                    r.session_id,
                    r.evidence_id,
                    r.report_version,
                    superseded=r.invalidated_at is not None,
                )
                for r in rows
            ]

        return await self._db.transaction(work)

    async def record_revalidation(
        self,
        owner_id: str,
        report_id: str,
        verdicts: Sequence[ReuseLinkVerdict],
        *,
        at: datetime,
        actor_id: str,
        audit_id: str,
    ) -> ReuseRevalidation:
        imports = session_report_evidence

        def work(connection: sa.Connection) -> ReuseRevalidation:
            report = connection.execute(
                sa.select(reports.c.deleted_at)
                .where(
                    reports.c.report_id == report_id,
                    reports.c.owner_id == owner_id,
                )
                .with_for_update(read=True)
            ).one_or_none()
            if report is None or report.deleted_at is not None:
                # Deleted again (or purged): its links stay withdrawn.
                return ReuseRevalidation(report_id)
            reinstated = 0
            refused: dict[str, int] = {}
            for verdict in verdicts:
                changes: dict[str, object] = {
                    "revalidated_at": at,
                    "revalidation_result": (
                        REVALIDATION_REINSTATED
                        if verdict.block is None
                        else verdict.block.value
                    ),
                }
                if verdict.block is None:
                    changes |= {"withdrawn_at": None, "withdrawn_reason": None}
                else:
                    changes["withdrawn_reason"] = WITHDRAWN_REVALIDATION_FAILED
                updated = connection.execute(
                    sa.update(imports)
                    .where(
                        imports.c.session_id == verdict.session_id,
                        imports.c.evidence_id == verdict.evidence_id,
                        imports.c.report_id == report_id,
                        imports.c.executive_id == owner_id,
                        imports.c.withdrawn_reason == WITHDRAWN_REVALIDATION_PENDING,
                    )
                    .values(**changes)
                ).rowcount
                if not updated:
                    continue
                if verdict.block is None:
                    reinstated += 1
                else:
                    refused[verdict.block.value] = (
                        refused.get(verdict.block.value, 0) + 1
                    )
            outcome = ReuseRevalidation(report_id, reinstated, refused)
            append_audit(
                connection,
                AuditEvent(
                    audit_id=audit_id,
                    occurred_at=at,
                    actor_id=actor_id,
                    action=REUSE_REVALIDATED,
                    subject_type="report",
                    subject_id=report_id,
                    details={
                        "owner_id": owner_id,
                        "links_reinstated": reinstated,
                        "links_refused": dict(sorted(refused.items())),
                    },
                ),
            )
            return outcome

        return await self._db.transaction(work)

    # --- FindingInvalidator -------------------------------------------------

    async def invalidate_dependent_findings(
        self, executive_id: str, session_id: str | None, slot: str
    ) -> None:
        def work(connection: sa.Connection) -> None:
            params = {
                "executive_id": executive_id,
                "session_id": session_id,
                "slot": slot,
                "now": self._db.clock(),
            }
            connection.execute(_INVALIDATE, params)
            if session_id is not None:
                connection.execute(_INVALIDATE_IMPORTS, params)

        await self._db.transaction(work)

    # --- EvidencePins -------------------------------------------------------

    async def pin(
        self, executive_id: str, evidence_ids: Sequence[str], holder: PinHolder
    ) -> None:
        wanted = set(evidence_ids)

        def work(connection: sa.Connection) -> None:
            owned = set(
                connection.execute(
                    sa.select(evidence.c.evidence_id).where(
                        evidence.c.evidence_id.in_(wanted),
                        evidence.c.executive_id == executive_id,
                    )
                ).scalars()
            )
            missing = wanted - owned
            if missing:
                raise AccessDenied("evidence", sorted(missing)[0])
            now = self._db.clock()
            try:
                # A savepoint, so a lost race leaves the transaction usable.
                with connection.begin_nested():
                    connection.execute(
                        pg_insert(evidence_pins).on_conflict_do_nothing(),
                        [
                            {
                                "evidence_id": evidence_id,
                                "holder_kind": holder.kind,
                                "holder_id": holder.holder_id,
                                "pinned_at": now,
                            }
                            for evidence_id in sorted(wanted)
                        ],
                    )
            except exc.IntegrityError:
                # The evidence expired and was cleaned up between the check
                # above and this insert: it is no longer available to pin.
                raise AccessDenied("evidence", sorted(wanted)[0]) from None

        if wanted:
            await self._db.transaction(work)

    async def unpin(self, holder: PinHolder) -> int:
        def work(connection: sa.Connection) -> int:
            result = connection.execute(
                sa.delete(evidence_pins).where(
                    evidence_pins.c.holder_kind == holder.kind,
                    evidence_pins.c.holder_id == holder.holder_id,
                )
            )
            return result.rowcount

        return await self._db.transaction(work)

    async def holders(self, evidence_id: str) -> tuple[PinHolder, ...]:
        def work(connection: sa.Connection) -> tuple[PinHolder, ...]:
            rows = connection.execute(
                sa.select(evidence_pins.c.holder_kind, evidence_pins.c.holder_id)
                .where(evidence_pins.c.evidence_id == evidence_id)
                .order_by(evidence_pins.c.holder_kind, evidence_pins.c.holder_id)
            )
            return tuple(PinHolder(r.holder_kind, r.holder_id) for r in rows)

        return await self._db.transaction(work)

    async def pinned(self, holder: PinHolder) -> tuple[str, ...]:
        def work(connection: sa.Connection) -> tuple[str, ...]:
            return tuple(
                connection.execute(
                    sa.select(evidence_pins.c.evidence_id)
                    .where(
                        evidence_pins.c.holder_kind == holder.kind,
                        evidence_pins.c.holder_id == holder.holder_id,
                    )
                    .order_by(evidence_pins.c.evidence_id)
                ).scalars()
            )

        return await self._db.transaction(work)
