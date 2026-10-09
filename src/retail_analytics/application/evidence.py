"""Evidence use cases: record immutable evidence and reuse it only when safe.

Query evidence is built only from a ``ReleasedResult`` (rows that already
passed the result privacy boundary) and its ``CompiledQuery``; it keeps the
model's analysis parameters but never the compiler's trusted or secret ones.
The authority stamp (authorization version and product-set digest) always
comes from the caller's freshly resolved ``ExecutionContext``; the exact
product set behind the digest is handed to the store with it (kept as a scope
snapshot for report access checks, never returned with the record).

Reuse goes through ``find_reusable``: candidates are loaded for the caller's
own session and every one is re-checked by ``ReusePolicy`` against the current
authority and the meaning the new calculation would have. If none qualifies,
the caller queries again (and passes ``refreshes`` to version the lineage).
An empty product scope never reads or reuses anything.

Report-owned retention uses ``pin_for``/``release_pins``; pins keep records
beyond the investigation but never authorize reading them.

Reusing a saved report in another session (T18-F2)
---------------------------------------------------
``import_report_evidence`` links the owner's report evidence into a run of
another of their sessions, with its source (report, version, title). There is
no grant, role or confirmation: each record must pass the normal automatic
checks: same owner, the owner's *current* products cover the exact set it
was computed under (trusted scope snapshots, T18-F1 rule), intact, not
invalidated, and its meaning still matches current definitions and settings
(``compatibility_block``). Imported records then count as session evidence
everywhere (``session_standing``, ``usable_in_session``, ``find_reusable``,
``link_to_run``), but are re-judged by ``ReusePolicy.imported_block`` on every
use, so losing a required product withholds them and the answers that cited
them. They stay historical snapshots: never refreshed, and current-data
reuse still applies the freshness limit.

Soft-deleted reports (T18-F5)
-----------------------------
Deleting a report withdraws, in the deletion transaction, every link it gave
another session. A record whose links are all withdrawn is withheld like any
record current authority no longer allows (``REPORT_LINK_WITHDRAWN``): not in
context, not citable, not fetchable, not reusable, and answers that used it
leave model context. So is session evidence derived from it. A record still
linked through another valid report, or obtained in the session itself, is
unaffected. Restoring the report only marks its links for re-validation;
``revalidate_report_links`` reinstates those that pass every import check
again and records the outcome.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import ClassVar

from retail_analytics.application.authorization import AccessDenied
from retail_analytics.application.contracts.evidence import (
    DEFAULT_CANDIDATE_LIMIT,
    ImportedEvidence,
    NewEvidence,
    NewEvidenceImport,
    ReuseLinkVerdict,
    ReuseRevalidation,
    StoredEvidence,
)
from retail_analytics.application.contracts.query_compiler import (
    CompiledQuery,
    QueryParameter,
)
from retail_analytics.application.contracts.tools import (
    ExecutionContext,
    OperationContext,
)
from retail_analytics.application.ports.evidence import (
    EvidencePins,
    EvidenceRepository,
    ProductScopeSnapshots,
    ReuseLinkRevalidation,
    SessionEvidenceImports,
)
from retail_analytics.application.result_privacy import ReleasedResult
from retail_analytics.domain.access import Permission, ProductScope
from retail_analytics.domain.context import EvidenceStanding
from retail_analytics.domain.evidence import (
    AnalysisCompatibility,
    AnalysisStamp,
    AuthorityStamp,
    CurrentAuthority,
    DefinitionRef,
    Evidence,
    EvidenceColumn,
    EvidenceContent,
    EvidenceError,
    EvidenceKind,
    EvidenceParameter,
    EvidenceTable,
    EvidenceUse,
    JsonValue,
    PinHolder,
    Provenance,
    ReportSource,
    Requirements,
    ReuseBlock,
    ReuseIntent,
    ReusePolicy,
    Snapshot,
    TermMeaning,
    canonical_json,
    check_bounds,
    compatibility_block,
    content_digest,
    scope_digest,
    snapshot,
)
from retail_analytics.domain.metric_preferences import resolve_term
from retail_analytics.domain.metrics import (
    REVENUE_TERM,
    MetricCatalog,
    MetricDefinition,
    Operation,
    UnknownMetricError,
)
from retail_analytics.domain.periods import DEFAULT_TIME_ZONE, DateWindow
from retail_analytics.domain.preferences import EffectivePreferences, PreferenceKind

# Logical fields a query's rows are dated by (UTC calendar dates).
DATED_FIELDS = frozenset({"ordered_date"})


class EvidenceRejected(Exception):
    """Evidence cannot be recorded. ``reason`` is a stable code; message safe."""

    _MESSAGES: ClassVar[dict[str, str]] = {
        "no_product_scope": "No product data is available to you",
        "stale_authorization": "Your access changed; the query must be run again",
        "input_unavailable": "A finding this depends on is no longer available",
        "invalid": "The result could not be recorded as evidence",
    }

    def __init__(self, reason: str) -> None:
        self.reason = reason
        self.message = self._MESSAGES.get(reason, self._MESSAGES["invalid"])
        super().__init__(self.message)


@dataclass(frozen=True, slots=True)
class QueryBasis:
    """Analytical meaning of a query result, supplied by trusted code.

    ``preference_fingerprint`` is ``EffectivePreferences.analytical_fingerprint``
    for the run; ``analytical_slots`` are the preference slots the calculation
    depended on (e.g. ``metric_definition:revenue``, ``time_zone``), so a later
    change to one of them invalidates it.
    """

    definitions: frozenset[DefinitionRef]
    preference_fingerprint: str
    grain: tuple[str, ...] = ()
    period: DateWindow | None = None
    time_zone: str = DEFAULT_TIME_ZONE
    analytical_slots: frozenset[str] = frozenset()
    subject_key: str | None = None
    terms: frozenset[TermMeaning] = frozenset()
    date_basis: str | None = None
    # True only when trusted code determined the definition basis above.
    definitions_recorded: bool = False


def query_basis(
    compiled: CompiledQuery,
    *,
    metrics: MetricCatalog,
    effective: EffectivePreferences,
) -> QueryBasis:
    """The definition basis of a compiled query, from trusted data only.

    It is context, not certification: a definition is included because the
    query read its fields, not because the SQL was shown to implement it.

    ``definitions`` are the catalog definitions in force (current versions,
    plus versions the executive's preferences select) whose population and
    measure fields the compiled query read; ``terms`` say what each business
    term ("revenue" and any term the executive defined) meant, when its
    meaning is one of those definitions. The period is the compiler's exact
    date window (None when the query has none or several), the date basis the
    dated logical field it read, and the time zone UTC (warehouse dates are
    UTC calendar dates). Nothing here comes from the model's text.
    """
    read = {(f.relation, f.field) for f in compiled.fields}
    preferences = effective.definition_preferences(metrics)
    candidates = {metrics.get(m) for m in metrics.metric_ids()}
    for p in preferences:
        candidates.add(metrics.get(p.metric_id, p.metric_version))
    used = {d for d in candidates if _reads_definition(d, read, metrics)}
    keys = {d.key for d in used}
    names = {REVENUE_TERM} | {
        e.setting.term
        for e in effective.entries
        if e.setting.kind is PreferenceKind.METRIC_DEFINITION and e.setting.term
    }
    terms: set[TermMeaning] = set()
    for term in names:
        try:
            meaning = resolve_term(metrics, term, preferences).definition
        except UnknownMetricError:
            continue
        if meaning.key in keys:
            terms.add(TermMeaning(term, DefinitionRef(*meaning.key)))
    dated = sorted({field for _, field in read if field in DATED_FIELDS})
    return QueryBasis(
        definitions=frozenset(DefinitionRef(*d.key) for d in used),
        preference_fingerprint=effective.analytical_fingerprint,
        period=compiled.date_window,
        time_zone=DEFAULT_TIME_ZONE,
        analytical_slots=frozenset(f"metric_definition:{t.term}" for t in terms),
        terms=frozenset(terms),
        date_basis=dated[0] if len(dated) == 1 else None,
        definitions_recorded=True,
    )


def _reads_definition(
    definition: MetricDefinition, read: set[tuple[str, str]], metrics: MetricCatalog
) -> bool:
    relation = definition.relation.value
    if (relation, definition.population.status_field) not in read:
        return False
    match definition.operation:
        case Operation.SUM:
            return (relation, definition.measure_field or "") in read
        case Operation.COUNT_DISTINCT:
            return (relation, definition.distinct_field or "") in read
        case Operation.COUNT_ROWS:
            return True
        case Operation.RATIO:
            parts = (definition.numerator_id, definition.denominator_id)
            return all(
                p is not None and _reads_definition(metrics.get(p), read, metrics)
                for p in parts
            )


@dataclass(frozen=True, slots=True)
class ReuseRequest:
    """Exactly one of ``subject_key`` (same question) or ``evidence_id``."""

    intent: ReuseIntent
    requirements: Requirements
    subject_key: str | None = None
    evidence_id: str | None = None

    def __post_init__(self) -> None:
        if (self.subject_key is None) == (self.evidence_id is None):
            raise ValueError("give exactly one of subject_key or evidence_id")


@dataclass(frozen=True, slots=True)
class ReusedEvidence:
    evidence: Evidence
    snapshot: Snapshot


@dataclass(frozen=True, slots=True)
class ReuseOutcome:
    """``reused`` is None when the caller must query (again).

    ``considered`` lists the caller's own candidates and why each was not
    used. ``refresh_of`` is the newest such candidate: pass it as
    ``refreshes`` when recording the new result to version the lineage.
    """

    reused: ReusedEvidence | None
    considered: tuple[tuple[str, ReuseBlock], ...] = ()
    refresh_of: str | None = None


@dataclass(frozen=True, slots=True)
class ReportEvidenceImport:
    """What ``import_report_evidence`` did with each cited record.

    ``linked`` are usable in the run now (with their source); ``refused``
    name the first failed check (for example ``definitions_changed``):
    those figures must be recomputed, never presented as results.
    """

    linked: tuple[str, ...]
    refused: tuple[tuple[str, ReuseBlock], ...] = ()
    # Provenance line per linked record ("from saved report ..., computed ...").
    sources: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class SessionEvidence:
    """Session evidence with its current standing, newest computation first."""

    standings: tuple[EvidenceStanding, ...]
    # Evidence each requested run produced or reused.
    run_links: Mapping[str, frozenset[str]]

    @property
    def usable(self) -> tuple[Evidence, ...]:
        return tuple(s.evidence for s in self.standings if s.usable)


def query_subject_key(compiled: CompiledQuery) -> str:
    """Identity of "the same question": logical query plus analysis values."""
    document: JsonValue = {
        "sql": compiled.logical_sql,
        "parameters": [_parameter_json(p) for p in compiled.analysis_parameters],
    }
    return "q:" + hashlib.sha256(canonical_json(document).encode()).hexdigest()[:40]


class EvidenceService:
    def __init__(
        self,
        repository: EvidenceRepository,
        pins: EvidencePins,
        *,
        clock: Callable[[], datetime],
        new_id: Callable[[], str],
        policy: ReusePolicy | None = None,
        imports: SessionEvidenceImports | None = None,
        scopes: ProductScopeSnapshots | None = None,
        links: ReuseLinkRevalidation | None = None,
    ) -> None:
        """Without ``imports`` and ``scopes`` evidence is strictly
        session-scoped (report evidence cannot be reused elsewhere). Without
        ``links`` a restored report's withdrawn links are never re-validated
        (they stay withdrawn; reading the report again re-imports)."""
        self._repository = repository
        self._pins = pins
        self._links = links
        self._imports = imports if scopes is not None else None
        self._scopes = scopes
        self._clock = clock
        self._new_id = new_id
        self._policy = policy or ReusePolicy()

    @property
    def policy(self) -> ReusePolicy:
        return self._policy

    async def record_query(
        self,
        ctx: OperationContext,
        compiled: CompiledQuery,
        released: ReleasedResult,
        basis: QueryBasis,
        *,
        refreshes: str | None = None,
        computed_at: datetime | None = None,
    ) -> Evidence:
        """Record released query rows as evidence for this operation."""
        scope = ctx.execution.product_scope
        if scope.is_empty:
            raise EvidenceRejected("no_product_scope")
        if not (
            released.entitlement_version
            == compiled.entitlement_version
            == scope.entitlement_version
        ):
            raise EvidenceRejected("stale_authorization")
        if released.catalog_version != compiled.catalog_version:
            raise EvidenceRejected("invalid")
        try:
            content = EvidenceContent(
                kind=EvidenceKind.QUERY,
                subject_key=basis.subject_key or query_subject_key(compiled),
                analysis=AnalysisStamp(
                    catalog_version=compiled.catalog_version,
                    policy_version=released.policy_version,
                    definitions=basis.definitions,
                    preference_fingerprint=basis.preference_fingerprint,
                    period=basis.period,
                    time_zone=basis.time_zone,
                    terms=basis.terms,
                    date_basis=basis.date_basis,
                    definitions_recorded=basis.definitions_recorded,
                ),
                provenance=Provenance(
                    logical_sql=compiled.logical_sql,
                    parameters=tuple(
                        _parameter(p) for p in compiled.analysis_parameters
                    ),
                    relations=tuple(sorted(compiled.relations)),
                    executed_query_digest=hashlib.sha256(
                        compiled.sql.encode()
                    ).hexdigest(),
                ),
                table=_table(released),
                grain=basis.grain,
                analytical_slots=basis.analytical_slots,
            )
        except EvidenceError:
            raise EvidenceRejected("invalid") from None
        return await self._store(ctx, content, refreshes, computed_at)

    async def record(
        self,
        ctx: OperationContext,
        content: EvidenceContent,
        *,
        refreshes: str | None = None,
        computed_at: datetime | None = None,
    ) -> Evidence:
        """Record derived or external evidence (query rows use ``record_query``)."""
        if content.kind is EvidenceKind.QUERY:
            raise EvidenceRejected("invalid")
        if ctx.execution.product_scope.is_empty:
            raise EvidenceRejected("no_product_scope")
        return await self._store(ctx, content, refreshes, computed_at)

    async def _store(
        self,
        ctx: OperationContext,
        content: EvidenceContent,
        refreshes: str | None,
        computed_at: datetime | None,
    ) -> Evidence:
        execution = ctx.execution
        _require_analysis(execution)
        try:
            check_bounds(content)
        except EvidenceError:
            raise EvidenceRejected("invalid") from None
        authority = self._authority(execution)
        imported = (
            {
                s.evidence.evidence_id: s
                for s in await self._imported_standings(authority, limit=200)
            }
            if content.derived_from
            else {}
        )
        for input_id in content.derived_from:
            if input_id in imported:
                if not imported[input_id].usable:
                    raise EvidenceRejected("input_unavailable")
                continue
            stored = await self._repository.get(input_id)
            if stored is None or self._own_block(stored, authority):
                raise EvidenceRejected("input_unavailable")
        if refreshes is not None:
            previous = await self._repository.get(refreshes)
            if previous is None or (
                previous.evidence.executive_id,
                previous.evidence.session_id,
            ) != (authority.executive_id, authority.session_id):
                raise AccessDenied("evidence", refreshes)
        when = computed_at or self._clock()
        return await self._repository.record(
            NewEvidence(
                evidence_id=self._new_id(),
                executive_id=execution.executive_id,
                session_id=authority.session_id,
                run_id=execution.correlation.run_id,
                operation_id=ctx.operation_id,
                authority=AuthorityStamp.of(execution.product_scope),
                scope_products=execution.product_scope.product_ids,
                content=content,
                computed_at=when,
                content_digest=content_digest(content, when),
                refreshes=refreshes,
            )
        )

    async def find_reusable(
        self, ctx: ExecutionContext, request: ReuseRequest
    ) -> ReuseOutcome:
        """Newest evidence that may stand in for a new query, linked to the run."""
        _require_analysis(ctx)
        if ctx.product_scope.is_empty:
            return ReuseOutcome(None)
        authority = self._authority(ctx)
        if request.evidence_id is not None:
            found = await self._repository.get(request.evidence_id)
            stored: list[StoredEvidence] = [] if found is None else [found]
        else:
            stored = list(
                await self._repository.candidates(
                    authority.executive_id,
                    authority.session_id,
                    subject_key=request.subject_key,
                )
            )
        imported = await self._imported_standings(
            authority, evidence_id=request.evidence_id, subject_key=request.subject_key
        )
        verdicts: dict[str, ReuseBlock | None] = {
            s.evidence.evidence_id: s.block for s in imported
        }
        stored = [
            *(s for s in stored if s.evidence.evidence_id not in verdicts),
            *(StoredEvidence(s.evidence) for s in imported),
        ]
        stored.sort(
            key=lambda s: (s.evidence.computed_at, s.evidence.version), reverse=True
        )
        now = self._clock()
        considered: list[tuple[str, ReuseBlock]] = []
        for candidate in stored:
            evidence = candidate.evidence
            if evidence.evidence_id in verdicts:
                # Report evidence from another session: coverage rule, then
                # the same meaning/freshness rules as any other candidate.
                block = verdicts[evidence.evidence_id] or self._policy.suitability(
                    evidence,
                    requirements=request.requirements,
                    intent=request.intent,
                    now=now,
                )
            else:
                block = self._policy.assess(
                    evidence,
                    authority=authority,
                    requirements=request.requirements,
                    intent=request.intent,
                    now=now,
                    invalidated=candidate.invalidated,
                    source_withdrawn=candidate.source_withdrawn,
                )
            if block in (ReuseBlock.NOT_OWNED, ReuseBlock.OTHER_SESSION):
                # Someone else's record looks exactly like a missing one.
                continue
            if block is None:
                await self._repository.link_run(
                    ctx.correlation.run_id, evidence.evidence_id, EvidenceUse.REUSED
                )
                return ReuseOutcome(
                    ReusedEvidence(evidence, snapshot(evidence, now)),
                    tuple(considered),
                    _refresh_of(considered, verdicts),
                )
            considered.append((evidence.evidence_id, block))
        return ReuseOutcome(None, tuple(considered), _refresh_of(considered, verdicts))

    async def usable_in_session(
        self, ctx: ExecutionContext, *, limit: int = DEFAULT_CANDIDATE_LIMIT
    ) -> tuple[Evidence, ...]:
        """Session evidence that current authority still allows into context.

        Excludes records computed under another authorization version or
        product set, and invalidated ones. Meaning/freshness checks are the
        reuse request's job; this only answers "may it be shown at all".
        """
        _require_analysis(ctx)
        if ctx.product_scope.is_empty:
            return ()
        authority = self._authority(ctx)
        stored = await self._repository.candidates(
            authority.executive_id, authority.session_id, limit=limit
        )
        own = [s.evidence for s in stored if self._own_block(s, authority) is None]
        imported = [
            s.evidence
            for s in await self._imported_standings(authority, limit=limit)
            if s.usable
        ]
        return tuple(own + imported)

    async def session_standing(
        self,
        ctx: ExecutionContext,
        *,
        run_ids: Sequence[str] = (),
        limit: int = DEFAULT_CANDIDATE_LIMIT,
    ) -> SessionEvidence:
        """Every recent session record judged against current authority.

        Unlike ``usable_in_session`` this also returns what is now withheld
        (and why), plus the evidence each of ``run_ids`` produced or reused,
        so context assembly can tell which earlier messages may carry
        restricted figures. Withheld records are for that judgement only and
        must never be shown. Records linked to a run but outside ``limit``
        are loaded individually; any that cannot be loaded are omitted, and
        callers treat a linked ID without a standing as withheld.
        """
        _require_analysis(ctx)
        authority = self._authority(ctx)
        stored = list(
            await self._repository.candidates(
                authority.executive_id, authority.session_id, limit=limit
            )
        )
        own = {s.evidence.evidence_id for s in stored}
        imported = [
            s
            for s in await self._imported_standings(authority, limit=limit)
            if s.evidence.evidence_id not in own
        ]
        # Imported records must not be reloaded below as other-session ones.
        known = own | {s.evidence.evidence_id for s in imported}
        run_links: dict[str, frozenset[str]] = {}
        for run_id in dict.fromkeys(run_ids):
            links = await self._repository.for_run(run_id)
            run_links[run_id] = frozenset(link.evidence_id for link in links)
            for evidence_id in sorted(run_links[run_id] - known):
                found = await self._repository.get(evidence_id)
                if found is not None:
                    stored.append(found)
                    known.add(evidence_id)
        standings = [
            EvidenceStanding(s.evidence, self._own_block(s, authority)) for s in stored
        ]
        standings += imported
        standings.sort(
            key=lambda s: (s.evidence.computed_at, s.evidence.version), reverse=True
        )
        return SessionEvidence(tuple(standings), run_links)

    async def link_to_run(
        self, ctx: ExecutionContext, evidence_ids: Sequence[str]
    ) -> tuple[str, ...]:
        """Record that ``ctx``'s run used these records (put them in model
        context or cited them), as provenance for the run's messages.

        Only the caller's own records in the same session are linked; others
        are skipped as if missing. Returns the linked IDs. Linking does not
        authorize use: readers still judge each record by current authority.
        """
        _require_analysis(ctx)
        authority = self._authority(ctx)
        imported = {
            s.evidence.evidence_id
            for s in await self._imported_standings(authority, limit=200)
        }
        linked: list[str] = []
        for evidence_id in dict.fromkeys(evidence_ids):
            if evidence_id in imported:
                await self._repository.link_run(
                    ctx.correlation.run_id, evidence_id, EvidenceUse.REUSED
                )
                linked.append(evidence_id)
                continue
            stored = await self._repository.get(evidence_id)
            if stored is None or (
                stored.evidence.executive_id,
                stored.evidence.session_id,
            ) != (authority.executive_id, authority.session_id):
                continue
            await self._repository.link_run(
                ctx.correlation.run_id, evidence_id, EvidenceUse.REUSED
            )
            linked.append(evidence_id)
        return tuple(linked)

    async def pin_for(
        self, executive_id: str, evidence_ids: Sequence[str], holder: PinHolder
    ) -> None:
        """Retain the executive's own evidence for a holder such as a report.

        ``executive_id`` must come from trusted, already-authorized code.
        """
        if evidence_ids:
            await self._pins.pin(
                executive_id, tuple(dict.fromkeys(evidence_ids)), holder
            )

    async def owned_records(
        self, executive_id: str, evidence_ids: Sequence[str]
    ) -> tuple[Evidence, ...]:
        """The executive's own records by ID, in the order given.

        Reading is not authorization to *use* a record: the caller must judge
        each one against current authority (for a report, its product-set
        digest). An unknown or another owner's ID raises the same
        ``AccessDenied``, so IDs cannot be probed.
        """
        found: list[Evidence] = []
        for evidence_id in dict.fromkeys(evidence_ids):
            stored = await self._repository.get(evidence_id)
            if stored is None or stored.evidence.executive_id != executive_id:
                raise AccessDenied("evidence", evidence_id)
            found.append(stored.evidence)
        return tuple(found)

    async def release_pins(self, holder: PinHolder) -> int:
        return await self._pins.unpin(holder)

    async def import_report_evidence(
        self,
        ctx: ExecutionContext,
        *,
        report_id: str,
        report_version: int,
        report_title: str,
        evidence_ids: Sequence[str],
        compatibility: AnalysisCompatibility,
    ) -> ReportEvidenceImport:
        """Link the owner's evidence cited by a saved report version into
        ``ctx``'s run and session, as historical snapshots with their source.

        ``report_*`` must come from a report version the caller just read as
        its owner (trusted code, never the model). Records already in this
        session are only linked to the run. Every other record passes the
        automatic checks (owner, current scope covers its computed scope,
        intact, not invalidated, meaning compatible) or is refused with the
        first failing rule; nothing needs a grant or a confirmation.
        """
        _require_analysis(ctx)
        authority = self._authority(ctx)
        if self._imports is None or authority.scope.is_empty:
            return ReportEvidenceImport(
                (),
                tuple(
                    (e, ReuseBlock.OTHER_SESSION) for e in dict.fromkeys(evidence_ids)
                ),
            )
        found: list[StoredEvidence] = []
        refused: list[tuple[str, ReuseBlock]] = []
        for evidence_id in dict.fromkeys(evidence_ids):
            stored = await self._repository.get(evidence_id)
            if stored is None or stored.evidence.executive_id != ctx.executive_id:
                refused.append((evidence_id, ReuseBlock.NOT_OWNED))
            else:
                found.append(stored)
        covered = await self._covered(
            authority.scope, {s.evidence.authority.scope_digest for s in found}
        )
        linked: list[str] = []
        sources: list[tuple[str, str]] = []
        source = ReportSource(report_id, report_version, report_title, self._clock())
        for stored in found:
            evidence = stored.evidence
            if evidence.session_id == authority.session_id:
                block = self._own_block(stored, authority)
            else:
                block = self._policy.imported_block(
                    evidence,
                    authority,
                    covered=evidence.authority.scope_digest in covered,
                    invalidated=stored.invalidated,
                )
            block = block or compatibility_block(evidence.content, compatibility)
            if block is not None:
                refused.append((evidence.evidence_id, block))
                continue
            if evidence.session_id == authority.session_id:
                await self._repository.link_run(
                    ctx.correlation.run_id, evidence.evidence_id, EvidenceUse.REUSED
                )
            elif not await self._imports.add_import(
                NewEvidenceImport(
                    session_id=authority.session_id,
                    evidence_id=evidence.evidence_id,
                    executive_id=ctx.executive_id,
                    run_id=ctx.correlation.run_id,
                    report_id=report_id,
                    report_version=report_version,
                    report_title=report_title,
                )
            ):
                # The report was soft-deleted after it was read.
                refused.append((evidence.evidence_id, ReuseBlock.REPORT_LINK_WITHDRAWN))
                continue
            linked.append(evidence.evidence_id)
            sources.append((evidence.evidence_id, source.describe(evidence)))
        return ReportEvidenceImport(tuple(linked), tuple(refused), tuple(sources))

    async def _imported_standings(
        self,
        authority: CurrentAuthority,
        *,
        evidence_id: str | None = None,
        subject_key: str | None = None,
        limit: int = DEFAULT_CANDIDATE_LIMIT,
    ) -> list[EvidenceStanding]:
        """Report evidence linked into the session, judged now by coverage."""
        if self._imports is None:
            return []
        found: Sequence[ImportedEvidence] = await self._imports.imported(
            authority.executive_id,
            authority.session_id,
            evidence_id=evidence_id,
            subject_key=subject_key,
            limit=limit,
        )
        covered = await self._covered(
            authority.scope, {i.evidence.authority.scope_digest for i in found}
        )
        return [
            EvidenceStanding(
                i.evidence,
                self._policy.imported_block(
                    i.evidence,
                    authority,
                    covered=i.evidence.authority.scope_digest in covered,
                    invalidated=i.invalidated,
                    withdrawn=i.withdrawn,
                ),
                i.source,
            )
            for i in found
        ]

    async def revalidate_report_links(
        self,
        *,
        owner_id: str,
        report_id: str,
        scope: ProductScope,
        readable_versions: frozenset[int],
        compatibility_for: Callable[[str], Awaitable[AnalysisCompatibility]],
        actor_id: str,
    ) -> ReuseRevalidation:
        """Re-validate a restored report's withdrawn links into the owner's
        other sessions and record the outcome (trusted code only: the
        restore flow, after the report is live again).

        Each link must pass what an import checks now: the report version it
        came from is readable by the owner (``readable_versions``, T18-F1),
        the owner's current products cover the record's (``scope``), the
        record is intact and not invalidated, and its meaning is compatible
        with current definitions and that session's settings. Failing links
        stay withdrawn, with the first failed rule recorded.
        """
        if self._links is None or self._imports is None:
            return ReuseRevalidation(report_id)
        pending = await self._links.pending_links(owner_id, report_id)
        if not pending:
            return ReuseRevalidation(report_id)
        records: dict[str, StoredEvidence | None] = {}
        for link in pending:
            if link.evidence_id not in records:
                records[link.evidence_id] = await self._repository.get(link.evidence_id)
        covered = await self._covered(
            scope,
            {s.evidence.authority.scope_digest for s in records.values() if s},
        )
        compatibility: dict[str, AnalysisCompatibility] = {}
        verdicts: list[ReuseLinkVerdict] = []
        for link in pending:
            stored = records[link.evidence_id]
            block: ReuseBlock | None
            if stored is None or stored.evidence.executive_id != owner_id:
                block = ReuseBlock.NOT_OWNED
            elif link.report_version not in readable_versions:
                block = ReuseBlock.AUTHORIZATION_CHANGED
            else:
                block = self._policy.imported_block(
                    stored.evidence,
                    CurrentAuthority(owner_id, link.session_id, scope),
                    covered=stored.evidence.authority.scope_digest in covered,
                    invalidated=stored.invalidated or link.superseded,
                )
                if block is None:
                    if link.session_id not in compatibility:
                        compatibility[link.session_id] = await compatibility_for(
                            link.session_id
                        )
                    block = compatibility_block(
                        stored.evidence.content, compatibility[link.session_id]
                    )
            verdicts.append(ReuseLinkVerdict(link.session_id, link.evidence_id, block))
        return await self._links.record_revalidation(
            owner_id,
            report_id,
            verdicts,
            at=self._clock(),
            actor_id=actor_id,
            audit_id=self._new_id(),
        )

    def _own_block(
        self, stored: StoredEvidence, authority: CurrentAuthority
    ) -> ReuseBlock | None:
        return self._policy.authority_block(
            stored.evidence,
            authority,
            invalidated=stored.invalidated,
            source_withdrawn=stored.source_withdrawn,
        )

    async def _covered(self, scope: ProductScope, digests: set[str]) -> frozenset[str]:
        if scope.is_empty or not digests:
            return frozenset()
        current = scope_digest(scope)
        others = digests - {current}
        found = (
            await self._scopes.covered(others, scope)
            if others and self._scopes is not None
            else frozenset()
        )
        return found | (digests & {current})

    @staticmethod
    def _authority(ctx: ExecutionContext) -> CurrentAuthority:
        return CurrentAuthority(
            ctx.executive_id, ctx.correlation.session_id, ctx.product_scope
        )


def _refresh_of(
    considered: Sequence[tuple[str, ReuseBlock]], imported: Mapping[str, object]
) -> str | None:
    """Newest considered record of this session's own lineage (a report
    snapshot from another session is never refreshed in place)."""
    return next((e for e, _ in considered if e not in imported), None)


def _require_analysis(ctx: ExecutionContext) -> None:
    if Permission.ANALYSIS_READ.value not in ctx.permissions:
        raise AccessDenied("permission", Permission.ANALYSIS_READ.value)


def _json_scalar(value: object) -> JsonValue:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise EvidenceError("unsupported parameter value")


def _parameter_type(p: QueryParameter) -> str:
    return p.type.value + ("[]" if p.array else "")


def _parameter_value(p: QueryParameter) -> JsonValue:
    if isinstance(p.value, tuple):
        return [_json_scalar(v) for v in p.value]
    return _json_scalar(p.value)


def _parameter_json(p: QueryParameter) -> JsonValue:
    return {"name": p.name, "type": _parameter_type(p), "value": _parameter_value(p)}


def _parameter(p: QueryParameter) -> EvidenceParameter:
    # analysis_parameters excludes trusted ones; re-assert for secrets anyway.
    if p.trusted or p.secret:
        raise EvidenceError("trusted parameters never enter evidence")
    return EvidenceParameter(p.name, _parameter_type(p), _parameter_value(p))


def _table(released: ReleasedResult) -> EvidenceTable:
    return EvidenceTable(
        columns=tuple(
            EvidenceColumn(
                c.name,
                c.role.value,
                tuple(f"{s.relation}.{s.field}" for s in c.sources),
            )
            for c in released.columns
        ),
        rows=released.rows,
        received_rows=released.received_rows,
        truncation=None if released.truncation is None else released.truncation.value,
        masked_cells=released.masked_cells,
    )
