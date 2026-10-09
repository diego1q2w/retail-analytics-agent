"""In-memory evidence store obeying the same contract as the PostgreSQL one."""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from retail_analytics.application.authorization import AccessDenied
from retail_analytics.application.contracts.evidence import (
    DEFAULT_CANDIDATE_LIMIT,
    ImportedEvidence,
    NewEvidence,
    NewEvidenceImport,
    RunEvidenceLink,
    StoredEvidence,
)
from retail_analytics.application.contracts.persistence import IdempotencyConflict
from retail_analytics.domain.access import ProductScope
from retail_analytics.domain.evidence import (
    Evidence,
    EvidenceUse,
    PinHolder,
    ReportSource,
    product_set_digest,
)


@dataclass
class Clock:
    now: datetime = datetime(2026, 10, 8, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


@dataclass
class Ids:
    prefix: str = "evd"
    count: int = 0

    def __call__(self) -> str:
        self.count += 1
        return f"{self.prefix}{self.count}"


@dataclass
class FakeEvidenceStore:
    clock: Callable[[], datetime] = field(default_factory=Clock)
    records: dict[str, Evidence] = field(default_factory=dict)
    invalidated: set[str] = field(default_factory=set)
    links: list[RunEvidenceLink] = field(default_factory=list)
    pins: set[tuple[str, PinHolder]] = field(default_factory=set)
    # ProductScopeSnapshots: exact sets by digest, written with each record.
    snapshots: dict[str, frozenset[str]] = field(default_factory=dict)
    # SessionEvidenceImports: (session, evidence) -> (import, imported_at).
    imports: dict[tuple[str, str], tuple[NewEvidenceImport, datetime]] = field(
        default_factory=dict
    )
    imports_invalidated: set[tuple[str, str]] = field(default_factory=set)
    session_owners: dict[str, str] = field(default_factory=dict)
    # Soft-deleted reports: their links are withdrawn (T18-F5).
    deleted_reports: set[str] = field(default_factory=set)

    async def record(self, new: NewEvidence) -> Evidence:
        for existing in self.records.values():
            if existing.operation_id == new.operation_id:
                if (existing.content_digest, existing.executive_id) != (
                    new.content_digest,
                    new.executive_id,
                ):
                    raise IdempotencyConflict("evidence", new.operation_id)
                return existing
        lineage, version = new.evidence_id, 1
        if new.refreshes is not None:
            previous = self.records.get(new.refreshes)
            if previous is None or (previous.executive_id, previous.session_id) != (
                new.executive_id,
                new.session_id,
            ):
                raise AccessDenied("evidence", new.refreshes)
            lineage = previous.lineage_id
            version = 1 + max(
                e.version for e in self.records.values() if e.lineage_id == lineage
            )
        for input_id in new.content.derived_from:
            source = self.records.get(input_id)
            if source is None or source.executive_id != new.executive_id:
                raise AccessDenied("evidence", new.evidence_id)
        record = Evidence(
            evidence_id=new.evidence_id,
            lineage_id=lineage,
            version=version,
            executive_id=new.executive_id,
            session_id=new.session_id,
            run_id=new.run_id,
            operation_id=new.operation_id,
            authority=new.authority,
            content=new.content,
            computed_at=new.computed_at,
            content_digest=new.content_digest,
        )
        digest = product_set_digest(new.scope_products)
        assert digest == new.authority.scope_digest
        self.snapshots[digest] = frozenset(new.scope_products)
        self.records[record.evidence_id] = record
        await self.link_run(new.run_id, record.evidence_id, EvidenceUse.PRODUCED)
        return record

    async def combine(self, digests: Collection[str]) -> str | None:
        wanted = set(digests)
        if not wanted or not wanted <= self.snapshots.keys():
            return None
        union = frozenset().union(*(self.snapshots[d] for d in wanted))
        digest = product_set_digest(union)
        self.snapshots[digest] = union
        return digest

    async def covered(
        self, digests: Collection[str], scope: ProductScope
    ) -> frozenset[str]:
        return frozenset(
            d
            for d in digests
            if d in self.snapshots and self.snapshots[d] <= scope.product_ids
        )

    def _stored(self, record: Evidence) -> StoredEvidence:
        return StoredEvidence(record, record.evidence_id in self.invalidated)

    async def get(self, evidence_id: str) -> StoredEvidence | None:
        record = self.records.get(evidence_id)
        return None if record is None else self._stored(record)

    async def candidates(
        self,
        executive_id: str,
        session_id: str,
        *,
        subject_key: str | None = None,
        limit: int = DEFAULT_CANDIDATE_LIMIT,
    ) -> Sequence[StoredEvidence]:
        found = [
            e
            for e in self.records.values()
            if (e.executive_id, e.session_id) == (executive_id, session_id)
            and (subject_key is None or e.content.subject_key == subject_key)
        ]
        found.sort(key=lambda e: (e.computed_at, e.version), reverse=True)
        return [self._stored(e) for e in found[:limit]]

    async def link_run(self, run_id: str, evidence_id: str, use: EvidenceUse) -> None:
        if any((x.run_id, x.evidence_id) == (run_id, evidence_id) for x in self.links):
            return
        self.links.append(RunEvidenceLink(run_id, evidence_id, use, self.clock()))

    async def for_run(self, run_id: str) -> Sequence[RunEvidenceLink]:
        return [x for x in self.links if x.run_id == run_id]

    async def invalidate_dependent_findings(
        self, executive_id: str, session_id: str | None, slot: str
    ) -> None:
        affected = {
            e.evidence_id
            for e in self.records.values()
            if e.executive_id == executive_id
            and (session_id is None or e.session_id == session_id)
            and slot in e.content.analytical_slots
        }
        while True:
            more = {
                e.evidence_id
                for e in self.records.values()
                if set(e.content.derived_from) & affected
            } - affected
            if not more:
                break
            affected |= more
        self.invalidated |= affected
        if session_id is not None:
            self.imports_invalidated |= {
                key
                for key, (new, _) in self.imports.items()
                if key[0] == session_id
                and new.executive_id == executive_id
                and slot in self.records[key[1]].content.analytical_slots
            }

    async def add_import(self, new: NewEvidenceImport) -> bool:
        record = self.records.get(new.evidence_id)
        owner = self.session_owners.get(new.session_id, new.executive_id)
        if record is None or not (record.executive_id == owner == new.executive_id):
            raise AccessDenied("evidence", new.evidence_id)
        if new.report_id in self.deleted_reports:
            return False
        self.imports.setdefault((new.session_id, new.evidence_id), (new, self.clock()))
        await self.link_run(new.run_id, new.evidence_id, EvidenceUse.REUSED)
        return True

    async def imported(
        self,
        executive_id: str,
        session_id: str,
        *,
        evidence_id: str | None = None,
        subject_key: str | None = None,
        limit: int = DEFAULT_CANDIDATE_LIMIT,
    ) -> Sequence[ImportedEvidence]:
        found = [
            ImportedEvidence(
                self.records[key[1]],
                ReportSource(new.report_id, new.report_version, new.report_title, at),
                key[1] in self.invalidated or key in self.imports_invalidated,
                withdrawn=new.report_id in self.deleted_reports,
            )
            for key, (new, at) in self.imports.items()
            if key[0] == session_id
            and new.executive_id == executive_id
            and self.records[key[1]].executive_id == executive_id
            and (evidence_id is None or key[1] == evidence_id)
            and (
                subject_key is None
                or self.records[key[1]].content.subject_key == subject_key
            )
        ]
        found.sort(key=lambda i: i.source.imported_at, reverse=True)
        return found[:limit]

    async def pin(
        self, executive_id: str, evidence_ids: Sequence[str], holder: PinHolder
    ) -> None:
        for evidence_id in evidence_ids:
            record = self.records.get(evidence_id)
            if record is None or record.executive_id != executive_id:
                raise AccessDenied("evidence", evidence_id)
        self.pins |= {(evidence_id, holder) for evidence_id in evidence_ids}

    async def unpin(self, holder: PinHolder) -> int:
        removed = {p for p in self.pins if p[1] == holder}
        self.pins -= removed
        return len(removed)

    async def holders(self, evidence_id: str) -> tuple[PinHolder, ...]:
        return tuple(sorted((h for e, h in self.pins if e == evidence_id), key=str))

    async def pinned(self, holder: PinHolder) -> tuple[str, ...]:
        return tuple(sorted(e for e, h in self.pins if h == holder))

    def tamper(self, evidence_id: str, **changes: object) -> None:
        """Simulate an out-of-band change to stored content (tests only)."""
        record = self.records[evidence_id]
        self.records[evidence_id] = replace(
            record,
            content=replace(record.content, **changes),  # type: ignore[arg-type]
        )
