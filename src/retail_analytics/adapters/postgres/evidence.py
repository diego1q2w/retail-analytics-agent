"""PostgreSQL evidence store: records, run links, invalidations and pins.

Evidence rows are append-only (an UPDATE trigger rejects changes); refreshes
insert the next version of a lineage. Each public method is one transaction.
The store checks ownership linkage it can see (refresh and pin owners) but
reuse decisions belong to ``EvidenceService`` and the domain policy.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy import exc
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.dialects.postgresql import insert as pg_insert

from retail_analytics.adapters.postgres.database import Database, violated_constraint
from retail_analytics.adapters.postgres.schema import (
    evidence,
    evidence_dependencies,
    evidence_invalidations,
    evidence_pins,
    run_evidence,
)
from retail_analytics.application.authorization import AccessDenied
from retail_analytics.application.evidence import (
    DEFAULT_CANDIDATE_LIMIT,
    NewEvidence,
    RunEvidenceLink,
    StoredEvidence,
)
from retail_analytics.application.persistence import IdempotencyConflict
from retail_analytics.domain.evidence import (
    AuthorityStamp,
    Evidence,
    EvidenceContent,
    EvidenceKind,
    EvidenceUse,
    PinHolder,
    decode_analysis,
    decode_provenance,
    decode_table,
    encode_analysis,
    encode_provenance,
    encode_table,
)

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
    return sa.select(evidence, deps, invalidated)


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
    return StoredEvidence(record, bool(m["invalidated"]))


class PostgresEvidenceStore:
    """Implements ``EvidenceRepository``, ``EvidencePins`` and the preference
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

    # --- FindingInvalidator -------------------------------------------------

    async def invalidate_dependent_findings(
        self, executive_id: str, session_id: str | None, slot: str
    ) -> None:
        def work(connection: sa.Connection) -> None:
            connection.execute(
                _INVALIDATE,
                {
                    "executive_id": executive_id,
                    "session_id": session_id,
                    "slot": slot,
                    "now": self._db.clock(),
                },
            )

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
