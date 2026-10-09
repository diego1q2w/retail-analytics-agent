"""Markdown for a saved report and for its evidence appendix.

The body is rendered from a validated draft plus facts read from the cited
evidence (kind, period, definitions, notes), so the basis of every figure is
stated by trusted code and not only by the model. Rendering is deterministic:
it uses no wall clock, so a retried save produces the same bytes.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from retail_analytics.domain.evidence import DefinitionRef, Evidence
from retail_analytics.domain.labels import label_caveat, present_rows
from retail_analytics.domain.reports import ReportDraft

type DefinitionDescriber = Callable[[DefinitionRef], str | None]

RECOMMENDATION_NOTE = (
    "Recommendations are proposals for you to weigh. They are not observed "
    "results and were not measured."
)


def cite(evidence_ids: Sequence[str]) -> str:
    return "".join(f" [{e}]" for e in evidence_ids)


def _cell(text: str) -> str:
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").strip()


def evidence_basis(evidence: Evidence, describe: DefinitionDescriber) -> list[str]:
    """Facts that say what an evidence record measures and how."""
    content = evidence.content
    lines = [
        f"- Kind: {content.kind.value}; computed "
        f"{evidence.computed_at.isoformat()} (version {evidence.version})."
    ]
    analysis = content.analysis
    if analysis.period is not None:
        lines.append(f"- Period: {analysis.period.describe()} ({analysis.time_zone}).")
    for definition in sorted(analysis.definitions):
        described = describe(definition)
        lines.append(f"- Definition {definition}: {described or 'no description'}")
    table = content.table
    if table.truncated:
        lines.append(
            f"- Truncated ({table.truncation}): {len(table.rows)} of "
            f"{table.received_rows} rows kept; figures cover only those rows."
        )
    lines.extend(f"- {key}: {value}" for key, value in content.provenance.notes)
    return lines


def render_report(
    draft: ReportDraft,
    evidence: Sequence[Evidence],
    describe: DefinitionDescriber,
) -> str:
    """The report's Markdown (without the evidence rows)."""
    out: list[str] = [f"# {draft.title}", "", "## Summary", "", draft.summary, ""]
    out += ["## Findings", ""]
    out += [f"{n}. {f.text}{cite(f.evidence)}" for n, f in enumerate(draft.findings, 1)]
    out.append("")
    if draft.definitions:
        out += ["## Definitions", ""]
        out += [f"- {d}" for d in draft.definitions]
        out.append("")
    if draft.limitations:
        out += ["## Limitations", ""]
        out += [f"- {x}" for x in draft.limitations]
        out.append("")
    if draft.action_items:
        out += ["## Recommended actions", "", f"_{RECOMMENDATION_NOTE}_", ""]
        out += [
            f"- **Recommendation:** {a.text}"
            + (f" (based on{cite(a.based_on)})" if a.based_on else "")
            for a in draft.action_items
        ]
        out.append("")
    out += ["## Evidence and data basis", ""]
    for record in evidence:
        out.append(f"### {record.evidence_id}")
        out.append("")
        out += evidence_basis(record, describe)
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def render_evidence_appendix(
    evidence: Sequence[Evidence], *, heading: str = "## Cited evidence rows"
) -> str:
    """Each record's rows as a table, with explicit labels for missing names."""
    out: list[str] = [heading, ""]
    for record in evidence:
        table = record.content.table
        out += [f"### {record.evidence_id}", ""]
        if not table.columns:
            out += ["_No columns._", ""]
            continue
        out.append("| " + " | ".join(_cell(c.name) for c in table.columns) + " |")
        out.append("|" + "---|" * len(table.columns))
        for row in present_rows(table):
            out.append("| " + " | ".join(_cell(v) for v in row) + " |")
        if not table.rows:
            out.append("")
            out.append("_The result had no rows._")
        caveat = label_caveat(table)
        if caveat:
            out += ["", f"_{caveat}_"]
        out.append("")
    return "\n".join(out).rstrip() + "\n"
