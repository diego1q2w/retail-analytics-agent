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
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import ClassVar

from retail_analytics.application.authorization import AccessDenied
from retail_analytics.application.contracts.evidence import (
    DEFAULT_CANDIDATE_LIMIT,
    NewEvidence,
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
)
from retail_analytics.application.result_privacy import ReleasedResult
from retail_analytics.domain.access import Permission
from retail_analytics.domain.context import EvidenceStanding
from retail_analytics.domain.evidence import (
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
    Requirements,
    ReuseBlock,
    ReuseIntent,
    ReusePolicy,
    Snapshot,
    canonical_json,
    check_bounds,
    content_digest,
    snapshot,
)
from retail_analytics.domain.periods import DEFAULT_TIME_ZONE, DateWindow


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
    ) -> None:
        self._repository = repository
        self._pins = pins
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
        for input_id in content.derived_from:
            stored = await self._repository.get(input_id)
            if stored is None or self._policy.authority_block(
                stored.evidence, authority, invalidated=stored.invalidated
            ):
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
            stored: Sequence[StoredEvidence] = () if found is None else (found,)
        else:
            stored = await self._repository.candidates(
                authority.executive_id,
                authority.session_id,
                subject_key=request.subject_key,
            )
        now = self._clock()
        considered: list[tuple[str, ReuseBlock]] = []
        for candidate in stored:
            evidence = candidate.evidence
            block = self._policy.assess(
                evidence,
                authority=authority,
                requirements=request.requirements,
                intent=request.intent,
                now=now,
                invalidated=candidate.invalidated,
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
                    considered[0][0] if considered else None,
                )
            considered.append((evidence.evidence_id, block))
        return ReuseOutcome(
            None, tuple(considered), considered[0][0] if considered else None
        )

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
        return tuple(
            s.evidence
            for s in stored
            if self._policy.authority_block(
                s.evidence, authority, invalidated=s.invalidated
            )
            is None
        )

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
        known = {s.evidence.evidence_id for s in stored}
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
            EvidenceStanding(
                s.evidence,
                self._policy.authority_block(
                    s.evidence, authority, invalidated=s.invalidated
                ),
            )
            for s in stored
        ]
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
        linked: list[str] = []
        for evidence_id in dict.fromkeys(evidence_ids):
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

    @staticmethod
    def _authority(ctx: ExecutionContext) -> CurrentAuthority:
        return CurrentAuthority(
            ctx.executive_id, ctx.correlation.session_id, ctx.product_scope
        )


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
