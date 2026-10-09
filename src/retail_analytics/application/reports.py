"""Saved reports: create, list, read, export and search for their owner.

Saving
------
A report is rendered from a validated ``ReportDraft`` and the cited evidence,
checked by the ``OutputPrivacyGate`` for destination REPORT, written as a
Markdown artifact, and recorded with its evidence links. The gate resolves
the caller's authority afresh and is called immediately before the first
write, so a report whose supporting evidence became inaccessible is discarded
(``OutputWithheld``), never saved with gaps. The same operation ID returns the
original version; the same ID with different content is a conflict.

Supporting evidence is pinned for the report (``EvidenceService.pin_for``) so
it outlives the investigation. Pinning only retains data.

Reading a pinned snapshot (decision, T18-F1)
---------------------------------------------
A pin never authorizes reading. A report version and its evidence are readable
only by the report's owner and only while the owner's *current* products cover
the version's **required scope**: the union of the exact product sets its cited
evidence was computed under. Those sets are stamped by trusted code when the
evidence is recorded (the execution context's ``ProductScope``, kept as a scope
snapshot keyed by the evidence's product-set digest) and combined when the
version is saved. They never come from the model or from the products that
happen to appear in result rows. Widening access keeps old reports readable,
removing a product the version does not require changes nothing, and removing
any required product withholds the content, title and snippets
(``ACCESS_CHANGED``; listings show the report without its title) until the
owner saves a new version from a fresh analysis. A version saved before
required scopes were recorded, whose set could not be recovered exactly from
trusted data, keeps the strict rule: readable only while the owner's current
product set equals the one recorded at save time (by digest). Authority is
judged on every read, list and search, and again for each evidence record on
read and export; there is no cached grant, and the report row never stores
access rights. The subset check runs in the snapshot store, so product IDs are
never loaded into reports, tool results or model context.

Reusing a report's evidence in another session (T18-F2)
--------------------------------------------------------
``reuse_in_run`` links the evidence a version cites into a run of another of
the owner's sessions, after the same automatic checks as reading plus a
compatibility check (catalog, result policy, current metric definition
versions and the session's analytical settings). No extra permission or
confirmation exists for it. A definition change never blocks *reading* the
report; it only stops its figures from being reused as results (they must be
recomputed). Reused records keep their source and computation date
(``ReportSource.describe``) wherever they appear.

Soft deletion withdraws that reuse at once (T18-F5, in the deletion
transaction); ``revalidate_restored`` re-checks the withdrawn links after a
manual restore and reinstates only those that pass, with the outcome audited.

Definition notices (T18-F3)
---------------------------
``read`` and ``export`` compare the definitions recorded with the cited
evidence (metric versions, what terms such as "revenue" meant) with the
reader's current definitions (their effective metric preferences, by
``resolve_term``, and the catalog's current versions). Differences become
display-time ``DefinitionNotice`` values on the returned document; evidence
without recorded definitions adds a neutral notice. The recorded
definitions are context for the fields the queries read, not proof the SQL
implemented them, and a notice never certifies that a metric was applied. The saved
artifact and version are never modified, and a notice never blocks reading.

Search
------
Search scans the owner's newest live reports (title, then Markdown content)
and only the ones whose access is current, so another owner's reports and
content outside the owner's present scope can never match or appear in a
snippet. Deleted reports are excluded by the repository (soft deletion belongs
to the deletion flow).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import replace

from retail_analytics.application.artifacts import ArtifactError, ArtifactService
from retail_analytics.application.authorization import AccessDenied, AccessResolver
from retail_analytics.application.contracts.authorization import Principal
from retail_analytics.application.contracts.evidence import ReuseRevalidation
from retail_analytics.application.contracts.reports import (
    CitedEvidence,
    ExportedReport,
    NewReportVersion,
    ReportAccess,
    ReportDocument,
    ReportListing,
    ReportMatch,
    ReportSearchResult,
    SavedReport,
)
from retail_analytics.application.evidence import (
    EvidenceService,
    ReportEvidenceImport,
)
from retail_analytics.application.output_privacy import (
    OutputDestination,
    OutputPrivacyGate,
    OutputSection,
)
from retail_analytics.application.ports.evidence import ProductScopeSnapshots
from retail_analytics.application.ports.preferences import PreferenceStore
from retail_analytics.application.ports.reports import ReportRepository
from retail_analytics.application.result_privacy import PRIVACY_POLICY_VERSION
from retail_analytics.domain.access import Permission, ProductScope
from retail_analytics.domain.artifacts import MARKDOWN
from retail_analytics.domain.currency import (
    DECLARED_QUALIFIER,
    unsupported_currency_marks,
)
from retail_analytics.domain.evidence import (
    AnalysisCompatibility,
    DefinitionRef,
    Evidence,
    PinHolder,
    scope_digest,
)
from retail_analytics.domain.labels import present_rows
from retail_analytics.domain.logical_catalog import default_logical_catalog
from retail_analytics.domain.metrics import MetricCatalog, UnknownMetricError
from retail_analytics.domain.preferences import EffectivePreferences
from retail_analytics.domain.report_definitions import (
    DefinitionNotice,
    definition_notices,
)
from retail_analytics.domain.report_markdown import (
    DefinitionDescriber,
    render_evidence_appendix,
    render_report,
)
from retail_analytics.domain.reports import (
    REPORT_PIN_KIND,
    ReportDraft,
    ReportError,
    ReportErrorCode,
    ReportVersion,
)

_ACCESS_CHANGED = (
    "Your product access no longer covers what this report was based on, so it "
    "cannot be shown. Re-run the analysis to save a new version."
)
LIST_LIMIT = 100
SEARCH_SCAN_LIMIT = 200
SEARCH_RESULT_LIMIT = 25
SEARCH_TERMS = 8
SNIPPET_CHARS = 160
_DATE_BASIS = {
    "ordered_date": "the UTC date the order was placed (orders.created_at)",
}


def metric_describer(catalog: MetricCatalog) -> DefinitionDescriber:
    """Plain-language definitions, including which date each metric is dated by."""

    def describe(ref: DefinitionRef) -> str | None:
        try:
            metric = catalog.get(ref.metric_id, ref.version)
        except UnknownMetricError:
            return None
        statuses = ", ".join(sorted(metric.population.qualifying_statuses))
        basis = _DATE_BASIS.get(metric.time_field, metric.time_field)
        return (
            f"{metric.description} Counts rows whose "
            f"{metric.population.status_field} is {statuses}; dated by {basis}."
        )

    return describe


def _normalize(text: str) -> str:
    return " ".join(text.casefold().split())


class ReportAccessRule:
    """Whether the owner's current products cover a saved version.

    ``required`` is the version's required-scope digest (None for a legacy
    version without a recoverable exact set) and ``legacy`` the digest of the
    owner's product set when it was saved. Ownership is checked by the caller.
    """

    def __init__(self, scopes: ProductScopeSnapshots) -> None:
        self._scopes = scopes

    async def readable(
        self, scope: ProductScope, versions: Sequence[tuple[str | None, str]]
    ) -> tuple[bool, ...]:
        if scope.is_empty:
            return tuple(False for _ in versions)
        current = scope_digest(scope)
        covered = await self._covered(
            scope, {r for r, _ in versions if r is not None}, current
        )
        return tuple(
            legacy == current if required is None else required in covered
            for required, legacy in versions
        )

    async def covered_stamps(
        self, scope: ProductScope, digests: Iterable[str]
    ) -> frozenset[str]:
        """The evidence authority digests whose product set the scope covers."""
        if scope.is_empty:
            return frozenset()
        return await self._covered(scope, set(digests), scope_digest(scope))

    async def _covered(
        self, scope: ProductScope, digests: set[str], current: str
    ) -> frozenset[str]:
        # The current set trivially covers itself (also for a set never
        # snapshotted); everything else is a subset check in the store.
        others = digests - {current}
        found = await self._scopes.covered(others, scope) if others else frozenset()
        return found | (digests & {current})


class ReportService:
    def __init__(
        self,
        repository: ReportRepository,
        artifacts: ArtifactService,
        evidence: EvidenceService,
        gate: OutputPrivacyGate,
        resolver: AccessResolver,
        metrics: MetricCatalog,
        scopes: ProductScopeSnapshots,
        preferences: PreferenceStore,
        *,
        catalog_version: int | None = None,
        declared_currency: str | None = None,
    ) -> None:
        self._repository = repository
        self._artifacts = artifacts
        self._evidence = evidence
        self._gate = gate
        self._resolver = resolver
        self._describe = metric_describer(metrics)
        self._metrics = metrics
        self._catalog_version = (
            catalog_version
            if catalog_version is not None
            else default_logical_catalog().version
        )
        self._scopes = scopes
        self._rule = ReportAccessRule(scopes)
        self._preferences = preferences
        self._declared_currency = declared_currency

    # --- create ---------------------------------------------------------------

    async def create(
        self,
        principal: Principal,
        run_id: str,
        draft: ReportDraft,
        *,
        operation_id: str,
        report_id: str | None = None,
        base_version: int | None = None,
    ) -> SavedReport:
        """Save a new report, or a new version of ``report_id``.

        ``operation_id`` is the idempotency key. Raises ``AccessDenied`` (not
        owned, no analysis or own-report-read permission, no product scope,
        unknown evidence),
        ``OutputWithheld`` (the output check failed under current authority)
        or ``ReportError``.
        """
        ctx = await self._resolver.context_for_run(principal, run_id)
        if (
            Permission.ANALYSIS_READ.value not in ctx.permissions
            or ctx.product_scope.is_empty
        ):
            raise AccessDenied("permission", Permission.ANALYSIS_READ.value)
        # A report the user could not reopen must not be saved.
        if Permission.REPORTS_READ_OWN.value not in ctx.permissions:
            raise AccessDenied("permission", Permission.REPORTS_READ_OWN.value)
        owner = ctx.executive_id
        digest = draft.digest(report_id, base_version)

        prior = await self._repository.find_by_idempotency_key(owner, operation_id)
        if prior is not None:
            if prior.draft_digest != digest:
                raise ReportError(
                    ReportErrorCode.IDEMPOTENCY_CONFLICT,
                    "this operation already saved a different report",
                )
            return SavedReport(prior, duplicate=True)

        expected_latest = 0
        if report_id is not None:
            current = await self._repository.get(owner, report_id)
            if current is None:
                raise AccessDenied("report", report_id)
            if base_version is not None and base_version != current.version:
                raise ReportError(
                    ReportErrorCode.STALE_BASE_VERSION,
                    f"the report is at version {current.version}",
                )
            expected_latest = current.version

        cited = draft.cited_evidence
        records = await self._evidence.owned_records(owner, cited)
        _require_supported_currency(draft, records, self._declared_currency)
        # Fresh authority check immediately before anything is written: the
        # gate re-resolves access and which evidence is usable right now.
        released = await self._gate.check(
            principal, run_id, _sections(draft), OutputDestination.REPORT
        )
        texts = {section.name: section.text for section in released}
        checked = _apply_texts(draft, texts)
        markdown = render_report(checked, records, self._describe)
        # Required scope: the trusted stamps of the cited evidence records
        # (never the draft, never the products in their rows).
        required = await self._scopes.combine(
            {record.authority.scope_digest for record in records}
        )

        artifact = await self._artifacts.save(
            owner,
            media_type=MARKDOWN,
            content=markdown.encode(),
            idempotency_key=operation_id,
            artifact_id=report_id,
        )
        saved_id = artifact.artifact_id
        await self._evidence.pin_for(owner, cited, PinHolder(REPORT_PIN_KIND, saved_id))
        version, created = await self._repository.add_version(
            NewReportVersion(
                owner_id=owner,
                report_id=saved_id,
                session_id=ctx.correlation.session_id,
                run_id=run_id,
                artifact_version=artifact.version,
                title=checked.title,
                evidence_ids=cited,
                scope_digest=scope_digest(ctx.product_scope),
                authorization_version=ctx.product_scope.entitlement_version,
                draft_digest=digest,
                idempotency_key=operation_id,
                expected_latest=expected_latest,
                required_scope_digest=required,
            )
        )
        return SavedReport(version, duplicate=not created)

    # --- list / versions -----------------------------------------------------

    async def list_reports(
        self,
        principal: Principal,
        *,
        session_id: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[ReportListing, ...]:
        """The owner's live reports, newest first, optionally one conversation's."""
        scope = await self._read_scope(principal)
        rows = await self._repository.latest(
            principal.executive_id,
            session_id=session_id,
            limit=_clamp(limit, 1, LIST_LIMIT),
            offset=max(offset, 0),
        )
        return await self._listings(rows, scope)

    async def versions(
        self, principal: Principal, report_id: str
    ) -> tuple[ReportListing, ...]:
        scope = await self._read_scope(principal)
        rows = await self._repository.versions(principal.executive_id, report_id)
        if not rows:
            raise AccessDenied("report", report_id)
        return await self._listings(rows, scope)

    # --- read / export -------------------------------------------------------

    async def read(
        self,
        principal: Principal,
        report_id: str,
        version: int | None = None,
        *,
        session_id: str | None = None,
    ) -> ReportDocument:
        """``session_id`` (the reader's current conversation, if any) adds its
        session-scoped preferences to the current definitions compared."""
        record, markdown, evidence = await self._open(principal, report_id, version)
        return ReportDocument(
            record,
            markdown,
            tuple(_cited(e) for e in evidence),
            await self._notices(principal, evidence, session_id),
        )

    async def export(
        self,
        principal: Principal,
        report_id: str,
        version: int | None = None,
        *,
        session_id: str | None = None,
    ) -> ExportedReport:
        """The report with the rows of its cited evidence, as one Markdown file."""
        record, markdown, evidence = await self._open(principal, report_id, version)
        body = markdown.rstrip() + "\n\n" + render_evidence_appendix(evidence)
        return ExportedReport(
            filename=f"report-{record.report_id[:8]}-v{record.version}.md",
            media_type=MARKDOWN,
            content=body.encode(),
            version=record,
            notices=await self._notices(principal, evidence, session_id),
        )

    async def reuse_in_run(
        self,
        principal: Principal,
        run_id: str,
        document: ReportDocument,
        *,
        preference_fingerprint: str,
    ) -> ReportEvidenceImport:
        """Make a version's evidence citable in ``run_id`` (another session of
        the same owner) as historical snapshots, after the automatic checks.

        ``document`` must come from ``read`` by the same principal;
        ``preference_fingerprint`` is the run's current effective analytical
        fingerprint (trusted preference service). Raises ``AccessDenied`` when
        the run or the report is not the caller's.
        """
        ctx = await self._resolver.context_for_run(principal, run_id)
        record = document.version
        if record.owner_id != ctx.executive_id:
            raise AccessDenied("report", record.report_id)
        compatibility = self._compatibility(preference_fingerprint)
        return await self._evidence.import_report_evidence(
            ctx,
            report_id=record.report_id,
            report_version=record.version,
            report_title=record.title,
            evidence_ids=record.evidence_ids,
            compatibility=compatibility,
        )

    async def revalidate_restored(
        self, report_id: str, *, owner_id: str, actor_id: str
    ) -> ReuseRevalidation:
        """Re-validate the reuse links a restored report's deletion withdrew.

        Trusted restore flow only (``LifecycleService.restore``, after the
        report is live again); never reachable from model output. Judged with
        the *owner's* current authority, whoever restored it: a link comes
        back only if the owner may still read the report version it came from
        (T18-F1), their products cover the record, the record is intact and
        not invalidated, and its meaning matches current definitions and the
        importing session's settings (T18-F3, unknown is not compatible).
        """
        scope = ProductScope(frozenset(), 0)
        readable: frozenset[int] = frozenset()
        try:
            access = await self._resolver.current_access(
                Principal(owner_id, frozenset())
            )
        except AccessDenied:
            access = None
        if access is not None and Permission.REPORTS_READ_OWN in access.permissions:
            scope = access.product_scope
            rows = await self._repository.versions(owner_id, report_id)
            flags = await self._rule.readable(scope, [_digests(r) for r in rows])
            readable = frozenset(
                r.version for r, ok in zip(rows, flags, strict=True) if ok
            )

        async def compatibility_for(session_id: str) -> AnalysisCompatibility:
            stored = await self._preferences.list_preferences(owner_id, session_id)
            return self._compatibility(
                EffectivePreferences.build(stored).analytical_fingerprint
            )

        return await self._evidence.revalidate_report_links(
            owner_id=owner_id,
            report_id=report_id,
            scope=scope,
            readable_versions=readable,
            compatibility_for=compatibility_for,
            actor_id=actor_id,
        )

    def _compatibility(self, preference_fingerprint: str) -> AnalysisCompatibility:
        return AnalysisCompatibility(
            catalog_version=self._catalog_version,
            policy_version=PRIVACY_POLICY_VERSION,
            preference_fingerprint=preference_fingerprint,
            current_definitions={
                metric_id: self._metrics.latest_version(metric_id)
                for metric_id in self._metrics.metric_ids()
            },
        )

    # --- search --------------------------------------------------------------

    async def search(
        self,
        principal: Principal,
        query: str,
        *,
        session_id: str | None = None,
        limit: int = 10,
    ) -> ReportSearchResult:
        """Owned, live, currently accessible reports whose title or content
        contains every term of ``query`` (case-insensitive)."""
        terms = [t for t in _normalize(query).split(" ") if t][:SEARCH_TERMS]
        if not terms:
            raise ReportError(ReportErrorCode.INVALID_REQUEST, "search text is empty")
        scope = await self._read_scope(principal)
        owner = principal.executive_id
        rows = await self._repository.latest(
            owner, session_id=session_id, limit=SEARCH_SCAN_LIMIT + 1
        )
        scan_limited = len(rows) > SEARCH_SCAN_LIMIT
        rows = rows[:SEARCH_SCAN_LIMIT]
        readable = await self._rule.readable(scope, [_digests(r) for r in rows])
        matches: list[ReportMatch] = []
        withheld = 0
        for row, available in zip(rows, readable, strict=True):
            if not available:
                withheld += 1
                continue
            try:
                content = await self._artifacts.read(
                    owner, row.report_id, row.artifact_version
                )
            except (ArtifactError, AccessDenied):
                continue
            text = content.content.decode(errors="replace")
            title_hit = all(t in _normalize(row.title) for t in terms)
            body_hit = all(t in _normalize(text) for t in terms)
            if not (title_hit or body_hit):
                continue
            matches.append(
                ReportMatch(
                    _listing(row, available=True),
                    "title" if title_hit else "content",
                    _snippet(text, terms[0]) if body_hit else "",
                )
            )
        matches.sort(key=lambda m: m.matched_in != "title")  # stable: newest first
        return ReportSearchResult(
            tuple(matches[: _clamp(limit, 1, SEARCH_RESULT_LIMIT)]),
            scanned=len(rows),
            withheld=withheld,
            scan_limited=scan_limited,
        )

    # --- internals -----------------------------------------------------------

    async def _notices(
        self,
        principal: Principal,
        evidence: Sequence[Evidence],
        session_id: str | None,
    ) -> tuple[DefinitionNotice, ...]:
        # The store returns only this executive's preferences (and those of
        # the named session when it is theirs), so nothing else can leak in.
        stored = await self._preferences.list_preferences(
            principal.executive_id, session_id
        )
        current = EffectivePreferences.build(stored).definition_preferences(
            self._metrics
        )
        return definition_notices(evidence, self._metrics, current)

    async def _listings(
        self, rows: Sequence[ReportVersion], scope: ProductScope
    ) -> tuple[ReportListing, ...]:
        readable = await self._rule.readable(scope, [_digests(r) for r in rows])
        return tuple(
            _listing(row, available=ok) for row, ok in zip(rows, readable, strict=True)
        )

    async def _read_scope(self, principal: Principal) -> ProductScope:
        access = await self._resolver.require_permission(
            principal, Permission.REPORTS_READ_OWN
        )
        return access.product_scope

    async def _open(
        self, principal: Principal, report_id: str, version: int | None
    ) -> tuple[ReportVersion, str, tuple[Evidence, ...]]:
        scope = await self._read_scope(principal)
        owner = principal.executive_id
        record = await self._repository.get(owner, report_id, version)
        if record is None:
            raise AccessDenied("report", report_id)
        (readable,) = await self._rule.readable(scope, [_digests(record)])
        if not readable:
            raise ReportError(ReportErrorCode.ACCESS_CHANGED, _ACCESS_CHANGED)
        try:
            evidence = await self._evidence.owned_records(owner, record.evidence_ids)
        except AccessDenied:
            raise ReportError(
                ReportErrorCode.EVIDENCE_UNAVAILABLE,
                "The evidence this report cites is no longer available.",
            ) from None
        covered = await self._rule.covered_stamps(
            scope, {item.authority.scope_digest for item in evidence}
        )
        for item in evidence:
            if not item.is_intact or item.authority.scope_digest not in covered:
                raise ReportError(
                    ReportErrorCode.ACCESS_CHANGED
                    if item.is_intact
                    else ReportErrorCode.EVIDENCE_UNAVAILABLE,
                    "The evidence this report cites cannot be shown.",
                )
        content = await self._artifacts.read(
            owner, record.report_id, record.artifact_version
        )
        return record, content.content.decode(), evidence


def _supported_currency_codes(records: Sequence[Evidence]) -> set[str]:
    """Display currencies of cited conversions. The source currency is never
    listed: an unconverted amount has no verified code to show."""
    return {
        value
        for record in records
        for key, value in record.content.provenance.notes
        if key == "display_currency" and value
    }


def _require_supported_currency(
    draft: ReportDraft, records: Sequence[Evidence], declared: str | None
) -> None:
    marks = unsupported_currency_marks(
        draft.texts(), _supported_currency_codes(records), declared
    )
    if marks:
        allowed = (
            f"{declared} only as '{declared} ({DECLARED_QUALIFIER})', or an ISO "
            "code from a cited conversion"
            if declared
            else "an ISO code only from a cited conversion"
        )
        raise ReportError(
            ReportErrorCode.INVALID_DRAFT,
            "the report shows a currency (" + ", ".join(marks) + ") its evidence "
            "does not support. Write amounts without a symbol; say 'source "
            f"currency not verified' for unconverted amounts; allowed: {allowed}.",
        )


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _digests(record: ReportVersion) -> tuple[str | None, str]:
    return record.required_scope_digest, record.scope_digest


def _listing(record: ReportVersion, *, available: bool) -> ReportListing:
    return ReportListing(
        report_id=record.report_id,
        version=record.version,
        title=record.title if available else None,
        created_at=record.created_at,
        session_id=record.session_id,
        evidence_count=len(record.evidence_ids),
        access=ReportAccess.AVAILABLE if available else ReportAccess.ACCESS_CHANGED,
    )


def _cited(evidence: Evidence) -> CitedEvidence:
    table = evidence.content.table
    period = evidence.content.analysis.period
    return CitedEvidence(
        evidence_id=evidence.evidence_id,
        kind=evidence.content.kind.value,
        computed_at=evidence.computed_at,
        period_start=None if period is None else period.start,
        period_end=None if period is None else period.end,
        truncated=table.truncated,
        columns=table.column_names,
        rows=present_rows(table),
    )


def _snippet(text: str, term: str) -> str:
    flat = " ".join(text.split())
    at = flat.casefold().find(term)
    if at < 0:
        return flat[:SNIPPET_CHARS]
    start = max(0, at - SNIPPET_CHARS // 2)
    return flat[start : start + SNIPPET_CHARS]


def _sections(draft: ReportDraft) -> list[OutputSection]:
    sections = [
        OutputSection("title", draft.title),
        OutputSection("summary", draft.summary),
    ]
    sections += [
        OutputSection(f"finding-{n}", f.text, f.evidence)
        for n, f in enumerate(draft.findings, 1)
    ]
    sections += [
        OutputSection(f"definition-{n}", d) for n, d in enumerate(draft.definitions, 1)
    ]
    sections += [
        OutputSection(f"limitation-{n}", x) for n, x in enumerate(draft.limitations, 1)
    ]
    sections += [
        OutputSection(f"action-{n}", a.text, a.based_on)
        for n, a in enumerate(draft.action_items, 1)
    ]
    return sections


def _apply_texts(draft: ReportDraft, texts: dict[str, str]) -> ReportDraft:
    """The draft with the checked text of each section (REPORT never alters text,
    but the saved report must be exactly what passed the check)."""
    return replace(
        draft,
        title=texts["title"],
        summary=texts["summary"],
        findings=tuple(
            replace(f, text=texts[f"finding-{n}"])
            for n, f in enumerate(draft.findings, 1)
        ),
        definitions=tuple(
            texts[f"definition-{n}"] for n in range(1, len(draft.definitions) + 1)
        ),
        limitations=tuple(
            texts[f"limitation-{n}"] for n in range(1, len(draft.limitations) + 1)
        ),
        action_items=tuple(
            replace(a, text=texts[f"action-{n}"])
            for n, a in enumerate(draft.action_items, 1)
        ),
    )
