"""Terminal rendering: plain text with optional styling (click strips styles
when output is not a terminal).

All server text is treated as data: control characters and escape sequences
are removed before display so generated or saved text cannot drive the
terminal. No private reasoning is ever received, so none is shown.
"""

from __future__ import annotations

import re
from typing import Any

import click

from retail_analytics.domain.citations import number_citations
from retail_analytics.interfaces.cli.client import ApiError, JsonObject, Unreachable

_ESCAPES = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?")
_DISCLOSURE = re.compile(
    r"disclos|assumption|caveat|limitation|truncat|data quality|note|basis|"
    r"definition|currency",
    re.I,
)
_ACTION = re.compile(r"action|next step|recommend|follow[- ]?up|to ?do|suggested", re.I)


def safe(text: object) -> str:
    """Strip terminal escapes and control characters (newlines/tabs kept)."""
    cleaned = _ESCAPES.sub("", str(text))
    return "".join(c for c in cleaned if c in "\n\t" or c.isprintable())


def one_line(text: object) -> str:
    return " ".join(safe(text).split())


# --- events ---

_QUERY_TOOL = "execute_analysis"
# Query errors the assistant can fix by rewriting the query (wire codes). A
# budget, access or warehouse failure is never shown as one of these.
_ADJUSTABLE = frozenset(
    {"INVALID_QUERY", "UNSUPPORTED_SQL", "FIELD_UNAVAILABLE", "INVALID_INPUT"}
)
QUERY_NEEDS_ADJUSTMENT = "That query needs adjustment; the investigation is continuing."
QUERY_ADJUSTING = "Adjusting the query to a supported form."
WORKING = "Working on it."
QUEUED = "Queued: it will run after the current investigation."
STEERED = "Sent as steering for the active run."
ANSWERED = "Answer sent; the investigation continues."


def _needs_adjustment(kind: str, tool: JsonObject) -> bool:
    return (
        kind == "tool.failed"
        and tool.get("capability") == _QUERY_TOOL
        and tool.get("error_code") in _ADJUSTABLE
    )


class EventFormatter:
    """Progress lines for one event stream.

    Remembers a query that needs adjustment, so that the next query the
    assistant actually starts reads as its correction. Nothing is announced
    in advance: the assistant may instead ask, answer or stop.
    """

    def __init__(self) -> None:
        self._adjusting = False

    def __call__(self, event: JsonObject) -> str | None:
        kind = str(event.get("kind", ""))
        tool = event.get("tool") or {}
        if kind.startswith("run."):
            self._adjusting = False
        elif _needs_adjustment(kind, tool):
            self._adjusting = True
        elif (
            kind == "tool.started"
            and tool.get("capability") == _QUERY_TOOL
            and self._adjusting
        ):
            self._adjusting = False
            return click.style(f"  > {QUERY_ADJUSTING}", fg="cyan")
        return format_event(event)


def format_event(event: JsonObject) -> str | None:
    """One progress line; None for events shown elsewhere."""
    kind = str(event.get("kind", ""))
    summary = one_line(event.get("summary", ""))
    tool = event.get("tool") or {}
    if kind == "run.started":
        return click.style(WORKING, dim=True)
    if kind == "analysis.progress":
        return click.style(f"  ... {summary}", dim=True)
    if kind == "tool.started":
        return click.style(f"  > {summary}", fg="cyan")
    if kind == "tool.retrying":
        return click.style(f"  ! retrying: {summary}", fg="yellow")
    if kind == "tool.pending":
        return click.style(f"  ... still running: {summary}", fg="yellow")
    if kind == "tool.outcome_unknown":
        return click.style(
            f"  ? {summary} (the outcome is not confirmed yet; it is being checked)",
            fg="yellow",
        )
    if kind == "tool.succeeded":
        return click.style(f"  ok {summary}", fg="green")
    if _needs_adjustment(kind, tool):
        # The code and details stay in the run's events and traces.
        return click.style(f"  ~ {QUERY_NEEDS_ADJUSTMENT}", fg="yellow")
    if kind == "tool.failed":
        code = one_line(tool.get("error_code") or "failed")
        return click.style(f"  x {summary} [{code}]", fg="red")
    if kind == "deletion.proposed":
        proposal = one_line(event.get("deletion_proposal_id") or "")
        return click.style(
            f"  {summary} Review it with: analytics deletion show {proposal}",
            fg="yellow",
        )
    if kind in ("input.required", "run.completed", "run.partial"):
        return None
    if kind == "run.failed":
        return click.style(f"The run failed: {summary}", fg="red")
    if kind == "run.cancelled":
        return click.style(f"Cancelled: {summary}", fg="yellow")
    return click.style(f"  {kind}: {summary}", dim=True)


def format_acknowledgement(receipt: JsonObject) -> str:
    """What the server accepted, from its receipt, before any progress event:
    a request that is starting, one queued behind the active run, steering
    for the active run, or the answer to its clarification question."""
    kind = receipt.get("kind")
    if kind == "answer":
        return ANSWERED
    if kind == "steering":
        return STEERED
    if receipt.get("run_id") is None:
        return QUEUED
    return click.style(WORKING, dim=True)


def format_question(question: str, question_id: str | None = None) -> str:
    del question_id
    return (
        click.style("The assistant needs an answer to continue:", bold=True)
        + "\n  "
        + click.style(safe(question).strip().replace("\n", "\n  "), fg="magenta")
    )


# --- markdown-ish text -------------------------------------------------------------


def render_markdown(text: str) -> str:
    """Light Markdown rendering: headings, bold, code, quotes and lists.

    Tables and plain paragraphs are shown as written. Disclosure sections are
    marked and action-item sections highlighted so neither is missed.
    """
    lines: list[str] = []
    section: str | None = None
    in_code = False
    for raw in safe(text).splitlines():
        stripped = raw.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            lines.append(click.style("    " + raw, dim=True))
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            title = heading.group(2).strip()
            if _ACTION.search(title):
                section = "action"
                lines.append("")
                lines.append(
                    click.style(f"== {title.upper()} ==", bold=True, fg="green")
                )
            elif _DISCLOSURE.search(title):
                section = "disclosure"
                lines.append("")
                lines.append(
                    click.style(f"== {title.upper()} ==", bold=True, fg="yellow")
                )
            else:
                section = None
                lines.append("")
                lines.append(click.style(title.upper(), bold=True))
            continue
        bullet = re.match(r"^(\s*)[-*+]\s+(.*)$", raw)
        if bullet:
            marker = "[ ]" if section == "action" else "-"
            body = _inline(bullet.group(2))
            lines.append(f"{bullet.group(1)}  {marker} {body}")
            continue
        if stripped.startswith(">"):
            lines.append(click.style("  | " + _inline(stripped[1:].strip()), dim=True))
            continue
        lines.append(_inline(raw))
    return "\n".join(lines).strip("\n")


def _inline(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", lambda m: click.style(m.group(1), bold=True), text)
    return re.sub(r"`([^`]+)`", lambda m: click.style(m.group(1), fg="cyan"), text)


# --- run results ---

PARTIAL_BANNER = (
    "PARTIAL RESULT: the request was not fully answered; the answer below says "
    "what stopped it and what is missing. Do not treat its figures as final."
)


def format_run_result(run: JsonObject) -> str:
    """The end of a run: status banner and the released answer, if any."""
    status = str(run.get("status", "?"))
    answer = run.get("answer")
    parts: list[str] = []
    if status == "partial":
        parts.append(click.style(PARTIAL_BANNER, bold=True, fg="yellow"))
    elif status == "failed":
        parts.append(click.style("The run failed.", bold=True, fg="red"))
    elif status == "cancelled":
        parts.append(
            click.style(
                "The run was cancelled. Work already finished may be kept; "
                "nothing further will start.",
                bold=True,
                fg="yellow",
            )
        )
    elif status == "cancelling":
        parts.append(
            click.style(
                "Cancellation requested. New work has stopped; the assistant is "
                "still confirming that external work (such as a running query) "
                "has stopped. Its final state is not known yet.",
                bold=True,
                fg="yellow",
            )
        )
    if isinstance(answer, dict):
        if answer.get("withheld"):
            parts.append(
                click.style(
                    "The answer was withheld by the privacy check: ",
                    bold=True,
                    fg="red",
                )
                + one_line(answer.get("text", ""))
            )
        else:
            parts.append(render_answer(answer))
    elif status in ("completed", "partial"):
        parts.append("(no answer text was released)")
    return "\n\n".join(parts)


# --- answer citations ---

_LABEL = re.compile(r"^S?[1-9][0-9]{0,3}$")
_EVIDENCE_ID = re.compile(r"^evd_[0-9a-z]{1,40}$")


def answer_citations(answer: JsonObject) -> list[JsonObject]:
    """The server's citation list, keeping only well-formed entries.

    The server recognized these against current access; nothing here adds
    a source the server did not list.
    """
    raw = answer.get("citations")
    if answer.get("withheld") or not isinstance(raw, list):
        return []
    valid: list[JsonObject] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        label, evidence_id = item.get("label"), item.get("evidence_id")
        if not (isinstance(label, str) and _LABEL.match(label)):
            continue
        if not (isinstance(evidence_id, str) and _EVIDENCE_ID.match(evidence_id)):
            continue
        if evidence_id in seen:
            continue
        seen.add(evidence_id)
        valid.append(item)
    return valid


def render_answer(answer: JsonObject) -> str:
    """A released answer with recognized evidence IDs shown as ``[1]`` and a
    Sources section (fresh, partial and reopened answers alike). IDs the
    server did not recognize stay as written; the stored text is unchanged."""
    citations = answer_citations(answer)
    text = safe(answer.get("text", ""))
    if citations:
        labels = {str(c["evidence_id"]): str(c["label"]) for c in citations}
        text = number_citations(text, labels)
    body = render_markdown(text)
    if not citations:
        return body
    return body + "\n\n" + format_sources(citations)


def format_sources(citations: list[JsonObject]) -> str:
    lines = [click.style("SOURCES", bold=True)]
    for c in citations:
        description = one_line(c.get("description") or "") or (
            "Details of this result were not provided."
        )
        line = f"[{c['label']}] {description}"
        if c.get("current") is False:
            line = click.style(line, fg="yellow")
        lines.append(line)
    return "\n".join(lines)


def format_run_line(run: JsonObject) -> str:
    return (
        f"{run.get('run_id')}  {run.get('status'):<17}  "
        f"{one_line(run.get('updated_at', ''))}"
    )


def format_sessions(sessions: list[JsonObject]) -> str:
    if not sessions:
        return "No sessions yet."
    return "\n".join(
        f"{s['session_id']}  last active {one_line(s.get('last_activity_at'))}"
        for s in sessions
    )


# --- reports ---


_HIDDEN_TITLE = {
    "privacy_withdrawn": (
        "(hidden: cites individual customer demographics, which are now shown "
        "only as group-level statistics)"
    ),
}


def format_report_list(reports: list[JsonObject]) -> str:
    if not reports:
        return "No saved reports."
    rows = [
        (
            str(r["report_id"]),
            f"v{r['version']}",
            one_line(r.get("created_at", ""))[:10],
            one_line(r["title"])
            if r.get("title") is not None
            else _HIDDEN_TITLE.get(
                str(r.get("access")),
                "(hidden: your product access changed since it was saved)",
            ),
        )
        for r in reports
    ]
    return table(["REPORT", "VER", "SAVED", "TITLE"], rows)


def format_report_search(result: JsonObject) -> str:
    matches = result.get("matches", [])
    lines = []
    for m in matches:
        report = m["report"]
        lines.append(
            f"{report['report_id']}  v{report['version']}  "
            f"{one_line(report.get('title') or '(hidden)')}\n"
            f"    matched in {one_line(m.get('matched_in'))}: "
            f"{one_line(m.get('snippet'))}"
        )
    if not matches:
        lines.append("No matching reports.")
    notes = []
    if result.get("withheld"):
        notes.append(
            f"{result['withheld']} report(s) were not searched because your product "
            "access or the privacy rules changed since they were saved"
        )
    if result.get("scan_limited"):
        notes.append("only your most recent reports were searched")
    if notes:
        lines.append(click.style("Note: " + "; ".join(notes) + ".", fg="yellow"))
    return "\n".join(lines)


def format_definition_notices(notices: list[JsonObject]) -> str:
    """Display-time notices (not part of the saved report), one per line."""
    return "\n".join(
        click.style("DEFINITIONS: ", fg="yellow", bold=True)
        + one_line(str(n.get("message", "")))
        for n in notices
    )


def format_report(doc: JsonObject) -> str:
    out = [
        click.style(one_line(doc["title"]), bold=True)
        + f"  (report {doc['report_id']}, version {doc['version']}, "
        f"saved {one_line(doc['created_at'])})",
        "",
    ]
    notices = doc.get("definition_notices") or []
    if notices:
        out += [format_definition_notices(notices), ""]
    out.append(render_markdown(str(doc.get("markdown", ""))))
    evidence = doc.get("evidence") or []
    if evidence:
        out += ["", click.style("EVIDENCE", bold=True)]
        for e in evidence:
            period = ""
            if e.get("period_start") or e.get("period_end"):
                period = f", period {e.get('period_start')} to {e.get('period_end')}"
            out.append(
                f"[{one_line(e['evidence_id'])}] {one_line(e['kind'])}, computed "
                f"{one_line(e['computed_at'])}{period}"
            )
            if e.get("truncated"):
                out.append(
                    click.style(
                        "  TRUNCATED: the result was cut off; these rows are not "
                        "the complete data.",
                        fg="yellow",
                        bold=True,
                    )
                )
            columns = [one_line(c) for c in e.get("columns", [])]
            rows = [[one_line(c) for c in r] for r in e.get("rows", [])]
            if columns:
                out.append(table(columns, [tuple(r) for r in rows], indent="  "))
    return "\n".join(out)


def format_versions(result: JsonObject) -> str:
    return format_report_list(result.get("reports", []))


# --- deletion ---


def format_deletion_preview(preview: JsonObject) -> str:
    """Exactly what will be deleted, from the server's record (never from the
    assistant's chat text)."""
    lines = [
        click.style(
            f"Deletion proposal {preview['proposal_id']} ({preview['status']}): "
            f"{preview['count']} report(s) would be deleted",
            bold=True,
            fg="red",
        )
    ]
    for item in preview.get("items", []):
        title = item.get("title")
        shown = one_line(title) if title is not None else "(title hidden)"
        lines.append(
            f"  - {item['report_id']}  v{item['version']}  saved "
            f"{one_line(item['created_at'])[:10]}  {shown}"
        )
    lines.append(f"  The proposal expires at {one_line(preview['expires_at'])}.")
    return "\n".join(lines)


def confirmation_phrase(count: int) -> str:
    return f"delete {count} report" + ("" if count == 1 else "s")


# --- tables and errors ---


def table(
    headers: list[str], rows: list[tuple[str, ...]] | list[Any], indent: str = ""
) -> str:
    cells = [[str(c) for c in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in cells:
        for i, cell in enumerate(row[: len(widths)]):
            widths[i] = max(widths[i], len(cell))

    def fmt(row: list[str]) -> str:
        return (
            indent
            + "  ".join(c.ljust(w) for c, w in zip(row, widths, strict=False)).rstrip()
        )

    return "\n".join([fmt(headers), *(fmt(r) for r in cells)])


_HINTS = {
    "unauthenticated": "Your token is missing, expired or invalid; get a new one.",
    "no_token": "",
    "active_run_exists": "Another investigation is running in this session.",
    "run_not_active": "That run has already finished.",
    "expired": "The proposal expired; ask the assistant to propose it again.",
    "already_resolved": "That proposal was already confirmed or cancelled.",
    "stale": "The reports changed since the proposal; ask for a new proposal.",
    "forbidden": "Your account lacks the permission for this.",
}


def format_error(error: ApiError | Unreachable) -> str:
    if isinstance(error, Unreachable):
        return (
            f"error [unreachable]: the backend could not be reached ({error.reason}). "
            "Check CLI_API_URL and that retail-analytics-api is running."
        )
    text = f"error [{error.code}]: {one_line(error.message)}"
    hint = _HINTS.get(error.code)
    if hint:
        text += f"\n  {hint}"
    active = error.details.get("active_run_id")
    if active:
        text += (
            f"\n  Active run: {one_line(active)} (analytics follow {one_line(active)})"
        )
    return text
