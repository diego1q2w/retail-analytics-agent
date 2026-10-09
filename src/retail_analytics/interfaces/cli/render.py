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

from retail_analytics.interfaces.cli.client import ApiError, JsonObject, Unreachable

_ESCAPES = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?")
_HEX_ID = re.compile(r"\b[0-9a-f]{32}\b")
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


def find_hex_ids(text: str) -> list[str]:
    seen: list[str] = []
    for found in _HEX_ID.findall(text):
        if found not in seen:
            seen.append(found)
    return seen


# --- events ---


def format_event(event: JsonObject) -> str | None:
    """One progress line; None for events shown elsewhere."""
    kind = str(event.get("kind", ""))
    summary = one_line(event.get("summary", ""))
    tool = event.get("tool") or {}
    if kind == "run.started":
        return click.style("Working on it.", dim=True)
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
    if kind == "tool.failed":
        code = one_line(tool.get("error_code") or "failed")
        return click.style(f"  x {summary} [{code}]", fg="red")
    if kind in ("input.required", "run.completed", "run.partial"):
        return None
    if kind == "run.failed":
        return click.style(f"The run failed: {summary}", fg="red")
    if kind == "run.cancelled":
        return click.style(f"Cancelled: {summary}", fg="yellow")
    return click.style(f"  {kind}: {summary}", dim=True)


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
    "PARTIAL RESULT: this answer is incomplete or based on truncated data. "
    "Do not treat its figures as final."
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
            parts.append(render_markdown(str(answer.get("text", ""))))
    elif status in ("completed", "partial"):
        parts.append("(no answer text was released)")
    return "\n\n".join(parts)


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
            else "(hidden: your product access changed since it was saved)",
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
            "access changed since they were saved"
        )
    if result.get("scan_limited"):
        notes.append("only your most recent reports were searched")
    if notes:
        lines.append(click.style("Note: " + "; ".join(notes) + ".", fg="yellow"))
    return "\n".join(lines)


def format_report(doc: JsonObject) -> str:
    out = [
        click.style(one_line(doc["title"]), bold=True)
        + f"  (report {doc['report_id']}, version {doc['version']}, "
        f"saved {one_line(doc['created_at'])})",
        "",
        render_markdown(str(doc.get("markdown", ""))),
    ]
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
            "Check ANALYTICS_CLI_API_URL and that retail-analytics-api is running."
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
