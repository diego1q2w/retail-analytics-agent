"""Permission-aware context selection for one model iteration.

``ContextBuilder.build`` assembles what the model may see for the next step
of a run: the current request, applicable preferences, evidence the caller
may still use and the relevant recent conversation, within a bounded budget.
It re-resolves the caller's authority on every call (call it again on every
attempt and iteration; never cache its result across an authorization
change), so narrowed product scope, revoked permissions, invalidated evidence
and topic resets take effect on the next iteration:

- evidence comes only from ``EvidenceService.session_standing`` and only
  records usable under current authority are rendered, newest first, with
  bounded rows; larger tables are compacted to a reference the model can
  fetch through the evidence tool;
- history messages pass ``HistoryRules`` (see ``domain.context``), then every
  text that enters context is screened for direct personal data and
  references that current evidence does not contain;
- tool and history text is quoted as untrusted data; quoting preserves
  structure but is not a security boundary, which is why authority and
  privacy are enforced here and in the output gate instead.

``reset_topic`` records a topic boundary: earlier messages and evidence leave
context but nothing is deleted (saved reports and retained evidence stay).
Request admission (off-topic decline) lives in ``domain.request_scope``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime

from retail_analytics.application.authorization import (
    AccessResolver,
    OwnershipGuard,
)
from retail_analytics.application.contracts.authorization import Principal
from retail_analytics.application.contracts.context import TopicReset
from retail_analytics.application.evidence import EvidenceService
from retail_analytics.application.ports.context import (
    MessageHistory,
    TopicResets,
)
from retail_analytics.application.preferences import PreferenceService
from retail_analytics.domain.context import (
    CHARS_PER_TOKEN,
    ContextBudget,
    HistoryRules,
    HistoryTreatment,
    standings_by_id,
)
from retail_analytics.domain.conversation import MessageRole
from retail_analytics.domain.disclosure import (
    MASK,
    Detection,
    DisclosureKind,
    ProtectedTerm,
    detected_terms,
    figures,
    mask,
    normalize,
    references,
    scan,
)
from retail_analytics.domain.evidence import Evidence, EvidenceCell
from retail_analytics.domain.labels import fallback_for, label_notes
from retail_analytics.domain.preferences import EffectivePreferences
from retail_analytics.domain.request_scope import Admission, assess_request

FIGURE_MASK = "[figure withheld]"
_ACCESS_NOTE = (
    "[Earlier answer withheld: it relied on data outside your current access.]"
)
_SUPERSEDED_NOTE = (
    "[Earlier answer superseded: its findings used a definition or setting "
    "that has since changed and need recalculation.]"
)


@dataclass(frozen=True, slots=True)
class ContextMessage:
    message_id: str
    role: MessageRole
    text: str
    treatment: HistoryTreatment
    created_at: datetime


@dataclass(frozen=True, slots=True)
class EvidenceDigest:
    """A bounded view of one usable evidence record."""

    evidence_id: str
    version: int
    computed_at: datetime
    period: str | None
    definitions: tuple[str, ...]
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    total_rows: int
    truncated_at_source: bool
    # True when only a reference is included (budget); fetch rows on demand.
    compacted: bool
    # Disclosures about display fallbacks (missing product names or brands).
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ContextOmissions:
    """What was left out and why (counts only; never the content)."""

    history_access_changed: int = 0
    history_superseded: int = 0
    history_before_reset: int = 0
    history_over_budget: int = 0
    evidence_withheld: int = 0
    evidence_before_reset: int = 0
    evidence_over_budget: int = 0
    request_truncated: bool = False
    masked: tuple[DisclosureKind, ...] = ()


@dataclass(frozen=True, slots=True)
class ModelContext:
    """What one model iteration may see. Built fresh; never persisted as-is."""

    session_id: str
    run_id: str
    authorization_version: int
    request: str
    admission: Admission
    preferences: tuple[str, ...]
    evidence: tuple[EvidenceDigest, ...]
    history: tuple[ContextMessage, ...]
    omissions: ContextOmissions
    estimated_tokens: int
    topic_reset_at: datetime | None = None
    # Opaque references the model may legitimately use (from usable evidence).
    permitted_references: frozenset[str] = field(default_factory=frozenset)

    def render(self) -> str:
        """Plain-text context blocks; untrusted content is quoted as data."""
        parts = [
            "<preferences>",
            *(_quote(p) for p in self.preferences),
            "</preferences>",
            "<evidence>",
        ]
        for item in self.evidence:
            parts.append(_render_evidence(item))
        parts.append("</evidence>")
        parts.append("<conversation>")
        for message in self.history:
            parts.append(f"[{message.role.value}] {_quote(message.text)}")
        parts.append("</conversation>")
        notes = _omission_notes(self.omissions)
        if notes:
            parts.append("<context_notes>")
            parts.extend(notes)
            parts.append("</context_notes>")
        parts.append("<request>")
        parts.append(_quote(self.request))
        parts.append("</request>")
        return "\n".join(parts)

    def __repr__(self) -> str:
        return (
            f"ModelContext(run={self.run_id!r}, history={len(self.history)}, "
            f"evidence={len(self.evidence)}, tokens~{self.estimated_tokens})"
        )


class ContextBuilder:
    def __init__(
        self,
        resolver: AccessResolver,
        guard: OwnershipGuard,
        history: MessageHistory,
        evidence: EvidenceService,
        preferences: PreferenceService,
        resets: TopicResets,
        *,
        budget: ContextBudget | None = None,
        protected_terms: Callable[[], Iterable[ProtectedTerm]] = tuple,
    ) -> None:
        self._resolver = resolver
        self._guard = guard
        self._history = history
        self._evidence = evidence
        self._preferences = preferences
        self._resets = resets
        self._budget = budget or ContextBudget()
        self._protected_terms = protected_terms

    async def build(
        self,
        principal: Principal,
        run_id: str,
        request: str,
        *,
        request_message_id: str | None = None,
        trace_id: str | None = None,
    ) -> ModelContext:
        """Context for the next step of ``run_id`` under current authority.

        Raises ``AccessDenied`` when the run is not the caller's or analysis is
        no longer permitted (nothing is assembled then).
        """
        budget = self._budget
        ctx = await self._resolver.context_for_run(principal, run_id, trace_id=trace_id)
        session_id = ctx.correlation.session_id
        reset = await self._resets.latest_reset(session_id)
        reset_at = reset.reset_at if reset else None
        messages = [
            m
            for m in await self._history.recent_messages(
                session_id, budget.history_scan
            )
            if m.message_id != request_message_id
        ]
        session = await self._evidence.session_standing(
            ctx,
            run_ids=[m.run_id for m in messages if m.run_id is not None],
            limit=budget.evidence_scan,
        )
        preferences = await self._preferences.effective(
            principal, session_id=session_id
        )
        rules = HistoryRules(
            standings_by_id(session.standings), session.run_links, reset_at
        )
        usable = [
            e for e in session.usable if reset_at is None or e.computed_at >= reset_at
        ]
        permitted = frozenset(r for e in usable for r in _references_in(e))
        protected = tuple(self._protected_terms()) + user_supplied_terms(
            [request, *(m.content for m in messages if m.role is MessageRole.USER)]
        )
        screen = _Screen(permitted, protected)

        request_text, request_truncated = _clip(
            screen.text(request), budget.max_request_chars
        )
        used = len(request_text)
        preference_lines = tuple(screen.text(p) for p in _preference_lines(preferences))
        used += sum(len(p) for p in preference_lines)

        digests: list[EvidenceDigest] = []
        over_budget_evidence = 0
        for evidence in usable:
            if len(digests) >= budget.max_evidence:
                over_budget_evidence += 1
                continue
            digest = _digest(evidence, budget.max_rows_per_evidence, screen)
            size = _evidence_size(digest)
            if used + size > budget.max_chars:
                digest = _compact(digest)
                size = _evidence_size(digest)
                if used + size > budget.max_chars:
                    over_budget_evidence += 1
                    continue
            digests.append(digest)
            used += size

        counts = {t: 0 for t in HistoryTreatment}
        selected: list[ContextMessage] = []
        over_budget_history = 0
        for message in reversed(messages):
            treatment = rules.treatment(message)
            counts[treatment] += 1
            if treatment in (
                HistoryTreatment.BEFORE_RESET,
                HistoryTreatment.WITHHELD_SUPERSEDED,
                HistoryTreatment.WITHHELD_ACCESS_CHANGED,
            ):
                continue
            if len(selected) >= budget.max_history_messages:
                over_budget_history += 1
                continue
            text = screen.text(message.content)
            if treatment is HistoryTreatment.STRIP_FIGURES:
                text = _strip_figures(text)
            text, _ = _clip(text, budget.max_message_chars)
            if used + len(text) > budget.max_chars:
                over_budget_history += 1
                continue
            selected.append(
                ContextMessage(
                    message.message_id,
                    message.role,
                    text,
                    treatment,
                    message.created_at,
                )
            )
            used += len(text)
        history = tuple(reversed(selected))

        withheld = sum(1 for s in session.standings if not s.usable)
        before_reset = sum(
            1
            for e in session.usable
            if reset_at is not None and e.computed_at < reset_at
        )
        omissions = ContextOmissions(
            history_access_changed=counts[HistoryTreatment.WITHHELD_ACCESS_CHANGED],
            history_superseded=counts[HistoryTreatment.WITHHELD_SUPERSEDED],
            history_before_reset=counts[HistoryTreatment.BEFORE_RESET],
            history_over_budget=over_budget_history,
            evidence_withheld=withheld,
            evidence_before_reset=before_reset,
            evidence_over_budget=over_budget_evidence,
            request_truncated=request_truncated,
            masked=tuple(sorted(screen.masked)),
        )
        ongoing = bool(history) or bool(digests)
        return ModelContext(
            session_id=session_id,
            run_id=run_id,
            authorization_version=ctx.product_scope.entitlement_version,
            request=request_text,
            admission=assess_request(request, ongoing_investigation=ongoing),
            preferences=preference_lines,
            evidence=tuple(digests),
            history=history,
            omissions=omissions,
            estimated_tokens=-(-used // CHARS_PER_TOKEN),
            topic_reset_at=reset_at,
            permitted_references=permitted,
        )

    async def reset_topic(
        self, principal: Principal, session_id: str, reset_id: str
    ) -> TopicReset:
        """Start a new topic in the caller's own session.

        Only context changes: messages, evidence, reports and preferences are
        all kept. ``reset_id`` makes a retried request idempotent.
        """
        access = await self._resolver.current_access(principal)
        await self._guard.session(access.executive_id, session_id)
        return await self._resets.record_reset(session_id, reset_id)


@dataclass
class _Screen:
    """Masks personal data and unknown references in text entering context."""

    permitted: frozenset[str]
    protected: tuple[ProtectedTerm, ...]
    masked: set[DisclosureKind] = field(default_factory=set)

    def text(self, raw: str) -> str:
        text = normalize(raw)
        detections = list(scan(text, self.protected))
        for mention in references(text):
            if mention.reference not in self.permitted:
                detections.append(
                    Detection(
                        mention.start, mention.end, DisclosureKind.OPAQUE_REFERENCE
                    )
                )
        if not detections:
            return text
        self.masked.update(d.kind for d in detections)
        return mask(text, detections)


def user_supplied_terms(texts: Iterable[str]) -> tuple[ProtectedTerm, ...]:
    """Names and contact details a user typed, to keep out of later text.

    User input never authorizes lookup by a direct identifier, and what a
    user typed must not be echoed into answers, reports or memory either.
    """
    terms: dict[str, ProtectedTerm] = {}
    for raw in texts:
        text = normalize(raw)
        for term in detected_terms(text, scan(text)):
            terms.setdefault(term.text.lower(), term)
    return tuple(terms.values())


def _references_in(evidence: Evidence) -> Iterable[str]:
    table = evidence.content.table
    for index, column in enumerate(table.columns):
        if column.role != "reference":
            continue
        for row in table.rows:
            cell = row[index]
            if isinstance(cell, str):
                yield cell


def _strip_figures(text: str) -> str:
    spans = [
        Detection(f.start, f.end, DisclosureKind.RAW_IDENTIFIER)
        for f in figures(text)
        # Years carry dates, not results.
        if not (f.decimals == 0 and f.scale == 1 and 1900 <= f.value <= 2100)
    ]
    spans += [
        Detection(m.start, m.end, DisclosureKind.MALFORMED_REFERENCE)
        for m in references(text)
    ]
    return mask(text, spans, FIGURE_MASK)


def _clip(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[: max(limit - 1, 0)] + "…", True


def _preference_lines(preferences: EffectivePreferences) -> list[str]:
    return [
        f"{entry.setting.slot} = {entry.setting.value} "
        f"({entry.scope.value}; {entry.provenance})"
        for entry in preferences.entries
    ]


def _cell_text(cell: EvidenceCell, fallback: str | None = None) -> str:
    """Cell text; a missing product name or brand shows its explicit fallback."""
    if cell is None:
        return fallback or ""
    if isinstance(cell, datetime):
        return cell.isoformat()
    return str(cell)


def _digest(evidence: Evidence, max_rows: int, screen: _Screen) -> EvidenceDigest:
    content = evidence.content
    table = content.table
    rows = tuple(
        tuple(
            # Reference cells are already in the permitted set.
            _cell_text(cell)
            if column.role == "reference"
            else screen.text(_cell_text(cell, fallback_for(column)))
            for cell, column in zip(row, table.columns, strict=True)
        )
        for row in table.rows[:max_rows]
    )
    period = content.analysis.period
    return EvidenceDigest(
        evidence_id=evidence.evidence_id,
        version=evidence.version,
        computed_at=evidence.computed_at,
        period=period.describe() if period else None,
        definitions=tuple(str(d) for d in sorted(content.analysis.definitions)),
        columns=table.column_names,
        rows=rows,
        total_rows=len(table.rows),
        truncated_at_source=table.truncated,
        compacted=False,
        notes=label_notes(table),
    )


def _compact(digest: EvidenceDigest) -> EvidenceDigest:
    return EvidenceDigest(
        evidence_id=digest.evidence_id,
        version=digest.version,
        computed_at=digest.computed_at,
        period=digest.period,
        definitions=digest.definitions,
        columns=digest.columns,
        rows=(),
        total_rows=digest.total_rows,
        truncated_at_source=digest.truncated_at_source,
        compacted=True,
        notes=digest.notes,
    )


def _evidence_size(digest: EvidenceDigest) -> int:
    return len(_render_evidence(digest))


def _render_evidence(item: EvidenceDigest) -> str:
    header = (
        f"evidence {item.evidence_id} v{item.version}; computed "
        f"{item.computed_at.isoformat()}"
        + (f"; period {item.period}" if item.period else "")
        + (f"; definitions {', '.join(item.definitions)}" if item.definitions else "")
        + f"; {item.total_rows} rows"
        + ("; incomplete result" if item.truncated_at_source else "")
    )
    lines = [header, "columns: " + " | ".join(_quote(c) for c in item.columns)]
    if item.compacted:
        lines.append("rows omitted for space; fetch this evidence by id if needed")
    else:
        lines.extend(" | ".join(_quote(c) for c in row) for row in item.rows)
        if len(item.rows) < item.total_rows:
            lines.append(f"... {item.total_rows - len(item.rows)} more rows by id")
    lines.extend(f"note: {note}" for note in item.notes)
    return "\n".join(lines)


def _quote(text: str) -> str:
    """Neutralize anything that could close or open a context block."""
    return text.replace("<", "\u2039").replace(">", "\u203a")


def _omission_notes(omissions: ContextOmissions) -> list[str]:
    notes: list[str] = []
    # Withheld answers leave a marker so the model knows to recompute.
    if omissions.history_access_changed:
        notes.append(_ACCESS_NOTE)
    if omissions.history_superseded:
        notes.append(_SUPERSEDED_NOTE)
    if omissions.history_before_reset or omissions.evidence_before_reset:
        notes.append("The user started a new topic; earlier context is excluded.")
    if omissions.evidence_withheld:
        notes.append(
            f"{omissions.evidence_withheld} earlier findings are not usable now "
            "(access changed or superseded); recompute instead of recalling them."
        )
    if omissions.history_over_budget or omissions.evidence_over_budget:
        notes.append("Older context omitted for space; retrieve evidence by id.")
    if omissions.masked:
        notes.append(f"Personal data was removed and shown as {MASK}.")
    return notes
