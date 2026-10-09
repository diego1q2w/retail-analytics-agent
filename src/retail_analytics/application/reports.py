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

Reading a pinned snapshot (decision)
------------------------------------
A pin never authorizes reading. A report and its evidence are readable only by
the report's owner and only while the owner's *current* product set equals the
set the evidence was computed under (compared by digest, the same data
boundary evidence reuse enforces). If entitlements were narrowed, or changed in
any way, the content, titles and snippets of that report are withheld
(``ACCESS_CHANGED``; listings show the report without its title) until the
owner saves a new version from a fresh analysis. Authority is judged on every
read, list and search, and again for each evidence record on read and export;
there is no cached grant, and the report row never stores access rights. A
version-only change of the entitlement counter with the same product set does
not revoke, because the data covered is identical.

Search
------
Search scans the owner's newest live reports (title, then Markdown content)
and only the ones whose access is current, so another owner's reports and
content outside the owner's present scope can never match or appear in a
snippet. Deleted reports are excluded by the repository (soft deletion belongs
to the deletion flow).
"""

from __future__ import annotations

from dataclasses import replace

from retail_analytics.application.artifacts import ArtifactError, ArtifactService
from retail_analytics.application.authorization import AccessDenied, AccessResolver
from retail_analytics.application.contracts.authorization import Principal
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
from retail_analytics.application.evidence import EvidenceService
from retail_analytics.application.output_privacy import (
    OutputDestination,
    OutputPrivacyGate,
    OutputSection,
)
from retail_analytics.application.ports.reports import ReportRepository
from retail_analytics.domain.access import Permission, ProductScope
from retail_analytics.domain.artifacts import MARKDOWN
from retail_analytics.domain.evidence import (
    DefinitionRef,
    Evidence,
    PinHolder,
    scope_digest,
)
from retail_analytics.domain.labels import present_rows
from retail_analytics.domain.metrics import MetricCatalog, UnknownMetricError
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


class ReportService:
    def __init__(
        self,
        repository: ReportRepository,
        artifacts: ArtifactService,
        evidence: EvidenceService,
        gate: OutputPrivacyGate,
        resolver: AccessResolver,
        metrics: MetricCatalog,
    ) -> None:
        self._repository = repository
        self._artifacts = artifacts
        self._evidence = evidence
        self._gate = gate
        self._resolver = resolver
        self._describe = metric_describer(metrics)

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
        owned, no analysis permission or product scope, unknown evidence),
        ``OutputWithheld`` (the output check failed under current authority)
        or ``ReportError``.
        """
        ctx = await self._resolver.context_for_run(principal, run_id)
        if (
            Permission.ANALYSIS_READ.value not in ctx.permissions
            or ctx.product_scope.is_empty
        ):
            raise AccessDenied("permission", Permission.ANALYSIS_READ.value)
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
        # Fresh authority check immediately before anything is written: the
        # gate re-resolves access and which evidence is usable right now.
        released = await self._gate.check(
            principal, run_id, _sections(draft), OutputDestination.REPORT
        )
        texts = {section.name: section.text for section in released}
        checked = _apply_texts(draft, texts)
        markdown = render_report(checked, records, self._describe)

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
        return tuple(_listing(row, scope) for row in rows)

    async def versions(
        self, principal: Principal, report_id: str
    ) -> tuple[ReportListing, ...]:
        scope = await self._read_scope(principal)
        rows = await self._repository.versions(principal.executive_id, report_id)
        if not rows:
            raise AccessDenied("report", report_id)
        return tuple(_listing(row, scope) for row in rows)

    # --- read / export -------------------------------------------------------

    async def read(
        self, principal: Principal, report_id: str, version: int | None = None
    ) -> ReportDocument:
        record, markdown, evidence = await self._open(principal, report_id, version)
        return ReportDocument(
            record,
            markdown,
            tuple(_cited(e) for e in evidence),
        )

    async def export(
        self, principal: Principal, report_id: str, version: int | None = None
    ) -> ExportedReport:
        """The report with the rows of its cited evidence, as one Markdown file."""
        record, markdown, evidence = await self._open(principal, report_id, version)
        body = markdown.rstrip() + "\n\n" + render_evidence_appendix(evidence)
        return ExportedReport(
            filename=f"report-{record.report_id[:8]}-v{record.version}.md",
            media_type=MARKDOWN,
            content=body.encode(),
            version=record,
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
        matches: list[ReportMatch] = []
        withheld = 0
        for row in rows:
            if not _scope_matches(row, scope):
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
                    _listing(row, scope),
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
        if not _scope_matches(record, scope):
            raise ReportError(
                ReportErrorCode.ACCESS_CHANGED,
                "Your product access changed since this report was saved, so it "
                "cannot be shown. Re-run the analysis to save a new version.",
            )
        try:
            evidence = await self._evidence.owned_records(owner, record.evidence_ids)
        except AccessDenied:
            raise ReportError(
                ReportErrorCode.EVIDENCE_UNAVAILABLE,
                "The evidence this report cites is no longer available.",
            ) from None
        for item in evidence:
            if not item.is_intact or not _stamp_matches(item, scope):
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


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _scope_matches(record: ReportVersion, scope: ProductScope) -> bool:
    return not scope.is_empty and record.scope_digest == scope_digest(scope)


def _stamp_matches(evidence: Evidence, scope: ProductScope) -> bool:
    return not scope.is_empty and evidence.authority.scope_digest == scope_digest(scope)


def _listing(record: ReportVersion, scope: ProductScope) -> ReportListing:
    available = _scope_matches(record, scope)
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
