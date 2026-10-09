"""Numbered sources for the evidence a released answer cites.

Read time only: nothing is stored. For an answer the output gate released,
the evidence IDs in its text are matched against what the caller may use now
and what the answer's run actually used:

- a citation is *recognized* only when it names a record linked to that run
  (produced, reused or cited by it) whose standing under the caller's current
  authority is usable, and which the release policy also counts as usable;
  a superseded record (same authority, changed definition or preference) is
  listed but marked as not current;
- anything else (an invented ID, another executive's or session's record,
  one withheld after narrowed access, a deleted report's link) is not
  recognized, gets no number and no description, and stays as written;
- a withheld answer has no citations at all (callers never ask).

Descriptions are written here from trusted metadata (kind, recorded
definition basis, period, time zone, date basis, computation time,
truncation, saved-report source, conversion rate provenance), never by a
model, and never include SQL, product entitlements, credentials or rows.
What a record did not record is said to be unknown, not guessed. Each
description passes the output gate like any displayed text; one it refuses
(or that cannot be built) becomes a neutral line.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta

from retail_analytics.application.authorization import AccessResolver
from retail_analytics.application.contracts.authorization import Principal
from retail_analytics.application.contracts.conversations import CitedSource
from retail_analytics.application.evidence import EvidenceService
from retail_analytics.application.output_privacy import (
    DisclosurePolicy,
    OutputDestination,
    OutputPrivacyGate,
    OutputSection,
    OutputWithheld,
)
from retail_analytics.domain.citations import (
    first_use_order,
    source_label,
    uses_numbered_prose,
)
from retail_analytics.domain.context import EvidenceStanding
from retail_analytics.domain.evidence import EvidenceKind
from retail_analytics.domain.periods import DateWindow

UNAVAILABLE_DESCRIPTION = "Details of this result cannot be shown."
SUPERSEDED_NOTE = (
    "superseded: a definition or preference it relied on changed after it was "
    "computed, so it does not reflect current settings"
)
_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
_KIND_LABELS = {
    EvidenceKind.QUERY: "Query result",
    EvidenceKind.DERIVED: "Calculated result",
    EvidenceKind.EXTERNAL: "External data",
}


class CitationSources:
    def __init__(
        self,
        *,
        resolver: AccessResolver,
        evidence: EvidenceService,
        gate: OutputPrivacyGate,
    ) -> None:
        self._resolver = resolver
        self._evidence = evidence
        self._gate = gate

    async def for_answer(
        self,
        principal: Principal,
        run_id: str,
        text: str,
        policy: DisclosurePolicy,
    ) -> tuple[CitedSource, ...]:
        """Numbered sources for ``text`` (the run's released answer).

        ``policy`` is the one the answer was released under; authority is
        resolved again here. Raises ``AccessDenied`` like any run read.
        """
        cited = first_use_order(text)
        if not cited or policy.run_evidence_withdrawn:
            return ()
        ctx = await self._resolver.context_for_run(principal, run_id, trace_id=run_id)
        session = await self._evidence.session_standing(ctx, run_ids=[run_id])
        linked = session.run_links.get(run_id, frozenset())
        standings = {s.evidence.evidence_id: s for s in session.standings}
        recognized: list[EvidenceStanding] = []
        for evidence_id in cited:
            standing = standings.get(evidence_id)
            if evidence_id not in linked or standing is None:
                continue
            current = standing.usable and evidence_id in policy.usable_evidence
            if current or standing.superseded:
                recognized.append(standing)
        prefixed = uses_numbered_prose(text)
        labels = {
            s.evidence.evidence_id: source_label(n, prefixed=prefixed)
            for n, s in enumerate(recognized, start=1)
        }
        return tuple(
            CitedSource(
                number=n,
                label=labels[s.evidence.evidence_id],
                evidence_id=s.evidence.evidence_id,
                kind=s.evidence.content.kind.value,
                description=self._released(policy, s, labels),
                current=not s.superseded,
            )
            for n, s in enumerate(recognized, start=1)
        )

    def _released(
        self,
        policy: DisclosurePolicy,
        standing: EvidenceStanding,
        labels: Mapping[str, str],
    ) -> str:
        try:
            text = describe_source(standing, labels)
            return self._gate.release(
                policy,
                OutputSection("source", text),
                OutputDestination.DISPLAY,
            ).text
        except (OutputWithheld, Exception):
            # Refused or unbuildable metadata never breaks the answer.
            return UNAVAILABLE_DESCRIPTION


def describe_source(standing: EvidenceStanding, labels: Mapping[str, str]) -> str:
    """One readable line from trusted metadata; unknowns are said to be so."""
    evidence = standing.evidence
    content = evidence.content
    if standing.source is not None:
        # The saved-report disclosure, unchanged: report, version, date and
        # "historical snapshot, not current data".
        line = standing.source.describe(evidence)
        parts = [line[:1].upper() + line[1:]]
    else:
        notes = dict(content.provenance.notes)
        if notes.get("kind") == "currency_conversion":
            parts = _conversion(content.derived_from, notes, labels)
        else:
            parts = [_KIND_LABELS.get(content.kind, "Result")]
            parts.append(_definitions(standing))
            parts.append(_period(standing))
            if content.grain:
                grouped = ", ".join(_words(g) for g in content.grain)
                parts.append("grouped by " + grouped)
            parts.append("computed " + _when(evidence.computed_at))
    if content.table.truncated:
        parts.append("incomplete: the result was cut off and rows are missing")
    if standing.superseded:
        parts.append(SUPERSEDED_NOTE)
    return "; ".join(p for p in parts if p) + "."


def _definitions(standing: EvidenceStanding) -> str:
    analysis = standing.evidence.content.analysis
    if not analysis.definitions_recorded:
        return "definition basis not recorded"
    if not analysis.definitions:
        return "no catalog metric definition involved"
    return "definition basis: " + ", ".join(
        f"{_words(d.metric_id)} v{d.version}" for d in sorted(analysis.definitions)
    )


def _period(standing: EvidenceStanding) -> str:
    analysis = standing.evidence.content.analysis
    if analysis.period is None:
        return "period not recorded"
    detail = [analysis.time_zone]
    if analysis.date_basis:
        detail.append("by " + _words(analysis.date_basis))
    return f"{_window(analysis.period)} ({', '.join(detail)})"


def _conversion(
    derived_from: tuple[str, ...], notes: Mapping[str, str], labels: Mapping[str, str]
) -> list[str]:
    source = derived_from[0] if derived_from else ""
    of = f"[{labels[source]}]" if source in labels else "an earlier result"
    target = notes.get("display_currency") or "an unrecorded currency"
    parts = [f"Currency conversion of {of} to {target}"]
    rate = notes.get("rate")
    if rate:
        provenance = [
            v
            for v in (
                notes.get("rate_source"),
                notes.get("rate_date") and f"rate date {notes['rate_date']}",
                notes.get("rate_method"),
            )
            if v
        ]
        parts.append(
            f"rate {rate}" + (f" ({', '.join(provenance)})" if provenance else "")
        )
    else:
        parts.append("rate not recorded")
    code = notes.get("source_currency")
    basis = notes.get("source_currency_basis")
    if code:
        parts.append(f"source currency {code}" + (f" ({basis})" if basis else ""))
    else:
        parts.append("source currency not verified")
    return parts


def _window(window: DateWindow) -> str:
    start = window.start
    if not window.is_empty and start.day == 1 and window.end == _next_month(start):
        return f"{_MONTHS[start.month - 1]} {start.year}"
    return window.describe()


def _next_month(day: date) -> date:
    return (day.replace(day=28) + timedelta(days=4)).replace(day=1)


def _when(moment: datetime) -> str:
    utc = moment.astimezone(UTC)
    return f"{utc.day} {_MONTHS[utc.month - 1]} {utc.year}, {utc:%H:%M} UTC"


def _words(name: str) -> str:
    return name.replace("_", " ").strip()
