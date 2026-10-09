"""Evidence citations in answer text, for numbered display.

Answers cite evidence by its stored ID (``evd_<hex>``), bracketed by the model
(``[evd_1]``, ``[evd_1, evd_2]``) or written plainly by application-authored
text (``Evidence: evd_1, evd_2`` and ``- evd_1: ...``). Display replaces each
*recognized* ID with a short number ``[1]`` assigned in first-use order; the
stored text and IDs never change.

The parser is bounded and conservative:

- only whole ``evd_`` tokens are considered (never part of a longer word);
- tokens inside fenced code blocks, inline code spans, URLs and Markdown link
  targets are left alone;
- an ID the caller did not recognize stays exactly as written, so rendering
  can never turn an invented or unavailable reference into a numbered source.

Collision rule: when the answer already contains numbered references of its
own (``[1]``, ``[2, 3]``) outside code, source numbers get an ``S`` prefix
(``[S1]``) so the two cannot be confused.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

# Same shape as stored and report-cited evidence IDs.
_ID = r"evd_[0-9a-z]{1,40}"
_TOKEN = re.compile(rf"(?<![\w/#=.@-]){_ID}(?![\w-])")
_GROUP = re.compile(rf"\[\s*{_ID}(?:\s*[,;]\s*{_ID})*\s*\]")
_GROUP_ID = re.compile(_ID)
_URL = re.compile(r"(?:\b[a-z][a-z0-9+.-]*://|\bwww\.)\S+|\]\([^)\s]*\)", re.I)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_NUMBERED_PROSE = re.compile(r"\[\d{1,4}(?:\s*,\s*\d{1,4})*\]")
# Bounds: answers are far shorter; anything beyond is shown as written.
MAX_SCANNED_CHARS = 100_000
MAX_CITATIONS = 100
SOURCE_PREFIX = "S"


@dataclass(frozen=True, slots=True)
class CitationMark:
    """One place in the text that cites evidence (a bracket group or a token)."""

    start: int
    end: int
    evidence_ids: tuple[str, ...]
    bracketed: bool


def _excluded_spans(text: str) -> list[tuple[int, int]]:
    """Code (fenced and inline), URLs and link targets: never rewritten."""
    spans: list[tuple[int, int]] = []
    offset = 0
    fence_start: int | None = None
    for line in text.splitlines(keepends=True):
        if line.strip().startswith("```"):
            if fence_start is None:
                fence_start = offset
            else:
                spans.append((fence_start, offset + len(line)))
                fence_start = None
        elif fence_start is None:
            for pattern in (_INLINE_CODE, _URL):
                spans.extend(
                    (offset + m.start(), offset + m.end())
                    for m in pattern.finditer(line)
                )
        offset += len(line)
    if fence_start is not None:
        spans.append((fence_start, len(text)))
    return spans


def _inside(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(s < end and start < e for s, e in spans)


def citation_marks(text: str) -> tuple[CitationMark, ...]:
    """Citation places in reading order (bounded; see the module notes)."""
    scanned = text[:MAX_SCANNED_CHARS]
    excluded = _excluded_spans(scanned)
    marks: list[CitationMark] = []
    taken: list[tuple[int, int]] = []
    for group in _GROUP.finditer(scanned):
        if _inside(group.start(), group.end(), excluded):
            continue
        ids = tuple(dict.fromkeys(_GROUP_ID.findall(group.group())))
        marks.append(CitationMark(group.start(), group.end(), ids, bracketed=True))
        taken.append((group.start(), group.end()))
    for token in _TOKEN.finditer(scanned):
        if _inside(token.start(), token.end(), excluded + taken):
            continue
        marks.append(
            CitationMark(token.start(), token.end(), (token.group(),), bracketed=False)
        )
    marks.sort(key=lambda m: m.start)
    return tuple(marks)


def first_use_order(text: str) -> tuple[str, ...]:
    """Distinct cited IDs in order of first appearance (at most MAX_CITATIONS)."""
    seen: dict[str, None] = {}
    for mark in citation_marks(text):
        for evidence_id in mark.evidence_ids:
            seen.setdefault(evidence_id)
    return tuple(seen)[:MAX_CITATIONS]


def uses_numbered_prose(text: str) -> bool:
    """The text already has its own ``[n]`` references outside code."""
    scanned = text[:MAX_SCANNED_CHARS]
    excluded = _excluded_spans(scanned)
    return any(
        not _inside(m.start(), m.end(), excluded)
        for m in _NUMBERED_PROSE.finditer(scanned)
    )


def source_label(number: int, *, prefixed: bool) -> str:
    """``1`` or, under the collision rule, ``S1`` (without brackets)."""
    return f"{SOURCE_PREFIX}{number}" if prefixed else str(number)


def number_citations(text: str, labels: Mapping[str, str]) -> str:
    """``text`` with each recognized ID shown as ``[label]``.

    ``labels`` is the authoritative mapping (evidence ID to ``1`` or ``S1``,
    see ``source_label``). IDs not in it are kept verbatim; a bracket group
    mixing both keeps the unrecognized IDs as written.
    """
    if not labels:
        return text
    out: list[str] = []
    position = 0
    for mark in citation_marks(text):
        if not any(i in labels for i in mark.evidence_ids):
            continue
        items = [labels.get(i, i) for i in mark.evidence_ids]
        out.append(text[position : mark.start])
        out.append("[" + ", ".join(dict.fromkeys(items)) + "]")
        position = mark.end
    out.append(text[position:])
    return "".join(out)
