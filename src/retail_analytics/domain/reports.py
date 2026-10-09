"""Saved reports: the structured draft a report is rendered from, and its record.

A report is a versioned interpretation, not evidence. Findings are observed
results and each cites the evidence it rests on. Recommended actions are
proposals and are rendered under their own heading, so a reader can always tell
what was measured from what is suggested. Report text is limited so it can be
checked, stored and exported predictably.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

REPORT_PIN_KIND = "report"
MAX_TITLE = 200
MAX_SUMMARY = 4000
MAX_ITEM = 1500
MAX_FINDINGS = 40
MAX_LIST_ITEMS = 25
MAX_EVIDENCE = 50

# Evidence IDs look like ``evd_<hex>``; any such token in free text must be one
# the report declares, or a citation could be forged past the output check.
EVIDENCE_TOKEN = re.compile(r"\bevd_?[0-9a-z]{1,40}\b", re.IGNORECASE)
_HEADING = re.compile(r"^\s{0,3}#{1,6}(\s|$)", re.MULTILINE)


class ReportErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    INVALID_DRAFT = "invalid_draft"
    UNDECLARED_CITATION = "undeclared_citation"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    STALE_BASE_VERSION = "stale_base_version"
    EVIDENCE_UNAVAILABLE = "evidence_unavailable"
    ACCESS_CHANGED = "access_changed"


class ReportError(Exception):
    """A report operation failed. ``message`` is safe to show the caller."""

    def __init__(self, code: ReportErrorCode, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code.value}: {message}")


def _single_line(label: str, text: str, limit: int = MAX_ITEM) -> str:
    cleaned = " ".join(text.split())
    if not cleaned:
        raise ReportError(ReportErrorCode.INVALID_DRAFT, f"{label} is empty")
    if len(cleaned) > limit:
        raise ReportError(
            ReportErrorCode.INVALID_DRAFT, f"{label} exceeds {limit} characters"
        )
    return cleaned


def _ids(label: str, values: tuple[str, ...], *, required: bool) -> tuple[str, ...]:
    unique = tuple(dict.fromkeys(values))
    if required and not unique:
        raise ReportError(
            ReportErrorCode.INVALID_DRAFT, f"{label} must cite at least one evidence ID"
        )
    if len(unique) > MAX_EVIDENCE or any(
        not v or len(v) > 64 or not EVIDENCE_TOKEN.fullmatch(v) for v in unique
    ):
        raise ReportError(
            ReportErrorCode.INVALID_DRAFT, f"{label} has invalid evidence IDs"
        )
    return unique


@dataclass(frozen=True, slots=True)
class Finding:
    """An observed result and the evidence it rests on."""

    text: str
    evidence: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", _single_line("finding", self.text))
        object.__setattr__(
            self, "evidence", _ids("a finding", self.evidence, required=True)
        )


@dataclass(frozen=True, slots=True)
class ActionItem:
    """A recommendation (not an observed result); optionally names its basis."""

    text: str
    based_on: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", _single_line("action item", self.text))
        object.__setattr__(
            self, "based_on", _ids("an action item", self.based_on, required=False)
        )


@dataclass(frozen=True, slots=True)
class ReportDraft:
    """What the model supplies. Evidence metadata is added by trusted code."""

    title: str
    summary: str
    findings: tuple[Finding, ...]
    definitions: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    action_items: tuple[ActionItem, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "title", _single_line("title", self.title, MAX_TITLE))
        summary = self.summary.strip()
        if not summary or len(summary) > MAX_SUMMARY:
            raise ReportError(
                ReportErrorCode.INVALID_DRAFT,
                f"summary must be 1-{MAX_SUMMARY} characters",
            )
        if _HEADING.search(summary) or _HEADING.search(self.title):
            raise ReportError(
                ReportErrorCode.INVALID_DRAFT,
                "text must not contain headings; sections are fixed",
            )
        object.__setattr__(self, "summary", summary)
        if not 0 < len(self.findings) <= MAX_FINDINGS:
            raise ReportError(
                ReportErrorCode.INVALID_DRAFT,
                f"a report needs 1-{MAX_FINDINGS} findings",
            )
        for label, items in (
            ("definitions", self.definitions),
            ("limitations", self.limitations),
            ("action items", self.action_items),
        ):
            if len(items) > MAX_LIST_ITEMS:
                raise ReportError(
                    ReportErrorCode.INVALID_DRAFT,
                    f"at most {MAX_LIST_ITEMS} {label}",
                )
        object.__setattr__(
            self,
            "definitions",
            tuple(_single_line("definition", d) for d in self.definitions),
        )
        object.__setattr__(
            self,
            "limitations",
            tuple(_single_line("limitation", x) for x in self.limitations),
        )
        declared = set(self.cited_evidence)
        if len(declared) > MAX_EVIDENCE:
            raise ReportError(
                ReportErrorCode.INVALID_DRAFT,
                f"a report cites at most {MAX_EVIDENCE} evidence records",
            )
        stray = {
            token
            for text in self.texts()
            for token in EVIDENCE_TOKEN.findall(text)
            if token not in declared
        }
        if stray:
            raise ReportError(
                ReportErrorCode.UNDECLARED_CITATION,
                "text mentions evidence IDs that are not cited by a finding or action",
            )

    @property
    def cited_evidence(self) -> tuple[str, ...]:
        """Every cited evidence ID, in order of first citation."""
        ordered: list[str] = []
        for finding in self.findings:
            ordered.extend(finding.evidence)
        for item in self.action_items:
            ordered.extend(item.based_on)
        return tuple(dict.fromkeys(ordered))

    def texts(self) -> tuple[str, ...]:
        return (
            self.title,
            self.summary,
            *(f.text for f in self.findings),
            *self.definitions,
            *self.limitations,
            *(a.text for a in self.action_items),
        )

    def digest(self, report_id: str | None, base_version: int | None) -> str:
        """Fingerprint of the request, to tell a retry from a different save."""
        payload = json.dumps(
            {
                "report_id": report_id,
                "base_version": base_version,
                "title": self.title,
                "summary": self.summary,
                "findings": [[f.text, list(f.evidence)] for f in self.findings],
                "definitions": list(self.definitions),
                "limitations": list(self.limitations),
                "actions": [[a.text, list(a.based_on)] for a in self.action_items],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ReportVersion:
    """One immutable saved version; the Markdown lives in the artifact store."""

    report_id: str
    version: int
    owner_id: str
    session_id: str | None
    run_id: str | None
    artifact_version: int
    title: str
    evidence_ids: tuple[str, ...]
    # The owner's product set when the version was saved (legacy access rule).
    scope_digest: str
    authorization_version: int
    draft_digest: str
    created_at: datetime
    # Digest of the version's required product scope: the union of the exact
    # product sets its cited evidence was computed under, recorded by trusted
    # code at save time (the IDs live only in the scope snapshot store). None
    # for a version saved before it was recorded whose set could not be
    # recovered exactly: such a version keeps the strict equal-digest rule.
    required_scope_digest: str | None = None

    def __post_init__(self) -> None:
        if self.version < 1 or self.artifact_version < 1:
            raise ValueError("versions start at 1")

    def __repr__(self) -> str:
        return f"ReportVersion({self.report_id!r}, v{self.version})"
