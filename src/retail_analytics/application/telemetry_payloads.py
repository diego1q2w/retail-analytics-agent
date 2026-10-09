"""Sanitizing and bounding interaction content before it reaches a trace.

Call sites hand :func:`capture` an explicit JSON-like structure built at a
known boundary (the messages sent to a provider, a tool's validated arguments,
a released answer); arbitrary objects are never serialized wholesale. The
result is a :class:`CapturedPayload` that is safe to export:

- every string goes through the same detectors as the output gate
  (``redact_for_telemetry``: emails, phones, addresses, cued names, raw
  customer/order keys, encoded identifiers, opaque references), then through
  secret shapes (API keys, bearer tokens, JWTs, ``key=value`` credentials) and
  long opaque runs; each replacement leaves a visible marker and is counted;
- values under secret-named keys (passwords, tokens, signatures, reasoning)
  and under direct-identifier column names are withheld outright;
- SQL (keys ``sql`` or ``*_sql``) loses its comments, and string literals that
  are not plainly dates or short codes are withheld; parameter values follow
  the same literal rule;
- size is bounded per string, per list and per payload, with an explicit
  ``[truncated: ...]`` marker wherever something was cut; unsupported values
  become ``[omitted: ...]``;
- if sanitizing fails for any reason the whole payload is replaced by an
  omission marker (fail closed): the analysis itself is never affected.

Masking is defense in depth, not anonymization: captured analytical context
stays sensitive and the trace store needs operator-only access.
"""

from __future__ import annotations

import itertools
import json
import math
import re
from collections.abc import Mapping, Sequence

from retail_analytics.application.contracts.telemetry import (
    CapturedPayload,
    PayloadSide,
    PayloadValue,
)
from retail_analytics.application.output_privacy import redact_for_telemetry
from retail_analytics.application.telemetry import SECRET_SHAPES
from retail_analytics.domain.catalog import DIRECT_IDENTIFIER_COLUMNS
from retail_analytics.domain.disclosure import MASK

MAX_PAYLOAD_CHARS = 32_000
MAX_STRING_CHARS = 20_000
MAX_ITEMS = 200
MAX_DEPTH = 10
# Hard ceiling for the serialized content (markers and JSON structure on top
# of the string budget); anything larger is omitted as a whole.
_HARD_LIMIT = MAX_PAYLOAD_CHARS + MAX_PAYLOAD_CHARS // 4

SECRET_MASK = "[redacted]"  # noqa: S105 - the marker text
FAILED = "[omitted: payload could not be sanitized]"

_SECRET_WORDS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "apikey",
        "token",
        "authorization",
        "cookie",
        "credential",
        "credentials",
        "signature",
        "signatures",
        "thought",
        "thoughts",
        "reasoning",
    }
)
_IDENTITY_KEYS = frozenset(DIRECT_IDENTIFIER_COLUMNS | {"user_id", "customer_id"})
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|key|password|"
    r"passwd|secret|client[_-]?secret)(\s*[=:]\s*)(?!\[)[^\s&,;'\"]{6,}"
)
# Long opaque runs that mix letters and digits (keys, signatures, tokens).
_OPAQUE = re.compile(
    r"(?<![A-Za-z0-9+/_=-])(?=[A-Za-z0-9+/_=-]*\d)(?=[A-Za-z0-9+/_=-]*[A-Za-z])"
    r"[A-Za-z0-9+/_=-]{32,}"
)
_SQL_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/|#[^\n]*", re.DOTALL)
_SQL_STRING = re.compile(r"'(?:[^'\\]|\\.|'')*'|\"(?:[^\"\\]|\\.)*\"")
_DATE_LITERAL = re.compile(
    r"\d{4}-\d{2}(?:-\d{2})?"
    r"(?:[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?"
)
_CODE_LITERAL = re.compile(r"[A-Za-z0-9_.%\-]{1,40}")
_WORDS_LITERAL = re.compile(r"[a-z0-9_.%\- ]{1,40}")
_PARAMETER_KEYS = frozenset({"parameters", "analysis_parameters"})
_IDENTITY_FILTER = re.compile(
    r"\b(" + "|".join(sorted(_IDENTITY_KEYS)) + r")\b"
    r"(\s*(?:=|<>|!=|\bIN\b|\bLIKE\b)\s*\(?\s*)"
    r"('(?:[^']|'')*'|\d+(?:\s*,\s*\d+)*)",
    re.IGNORECASE,
)


def capture(side: PayloadSide, value: object) -> CapturedPayload:
    """``value`` sanitized and bounded; never raises (fails closed)."""
    try:
        walker = _Walker()
        content = walker.walk(value, depth=0, mode=_Mode.TEXT)
        size = _size(content)
        if size > _HARD_LIMIT:
            return _omitted(side, "payload_too_large")
        return CapturedPayload(
            side=side,
            content=content,
            chars=size,
            redactions=walker.redactions,
            truncated=walker.truncated,
            omitted=tuple(sorted(walker.omitted)),
        )
    except Exception:
        return _omitted(side, "sanitization_failed")


def sanitize_text(text: str) -> tuple[str, int]:
    """Free text masked for telemetry, with the number of replacements."""
    masked = redact_for_telemetry(text)
    count = max(masked.count(MASK) - text.count(MASK), 0)
    masked, secrets = SECRET_SHAPES.subn(SECRET_MASK, masked)
    masked, assigned = _CREDENTIAL_ASSIGNMENT.subn(rf"\1\2{SECRET_MASK}", masked)
    masked, opaque = _OPAQUE.subn(SECRET_MASK, masked)
    return masked, count + secrets + assigned + opaque


def sanitize_sql(sql: str) -> tuple[str, int]:
    """SQL with comments removed and non-trivial string literals withheld."""
    without_comments, comments = _SQL_COMMENT.subn("/* [comment removed] */", sql)
    withheld = 0

    def literal(match: re.Match[str]) -> str:
        nonlocal withheld
        token = match.group(0)
        if _safe_literal(token[1:-1]):
            return token
        withheld += 1
        return f"{token[0]}{MASK}{token[0]}"

    filtered, identities = _IDENTITY_FILTER.subn(rf"\1\2'{MASK}'", without_comments)
    literals_checked = _SQL_STRING.sub(literal, filtered)
    text, masked = sanitize_text(literals_checked)
    return text, comments + identities + withheld + masked


def _safe_literal(value: str) -> bool:
    if _DATE_LITERAL.fullmatch(value):
        return True
    if not (_CODE_LITERAL.fullmatch(value) or _WORDS_LITERAL.fullmatch(value)):
        # Several capitalized words may be a person's name: never kept.
        return False
    return sanitize_text(value)[1] == 0


def _omitted(side: PayloadSide, reason: str) -> CapturedPayload:
    content = f"[omitted: {reason}]" if reason != "sanitization_failed" else FAILED
    return CapturedPayload(
        side=side, content=content, chars=len(content), omitted=(reason,)
    )


def _size(content: PayloadValue) -> int:
    return len(json.dumps(content, ensure_ascii=False))


class _Mode:
    TEXT = 0
    SQL = 1
    LITERAL = 2


def _key_words(key: str) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9]+", key.lower()) if w]


def _secret_key(key: str) -> bool:
    words = _key_words(key)
    if any(word in _SECRET_WORDS for word in words):
        return True
    return ("api", "key") in set(itertools.pairwise(words))


def _child_mode(key: str, mode: int) -> int:
    lowered = key.lower()
    if lowered == "sql" or lowered.endswith("_sql"):
        return _Mode.SQL
    if lowered in _PARAMETER_KEYS:
        return _Mode.LITERAL
    return mode


class _Walker:
    def __init__(self) -> None:
        self.left = MAX_PAYLOAD_CHARS
        self.redactions = 0
        self.truncated = False
        self.omitted: set[str] = set()

    def walk(self, value: object, *, depth: int, mode: int) -> PayloadValue:
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            if math.isfinite(value):
                return value
            self.omitted.add("non_finite_number")
            return "[omitted: non-finite number]"
        if isinstance(value, str):
            return self._string(value, mode)
        if depth >= MAX_DEPTH:
            self.truncated = True
            return "[truncated: nesting limit]"
        if isinstance(value, Mapping):
            return self._mapping(value, depth=depth, mode=mode)
        if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
            return self._sequence(value, depth=depth, mode=mode)
        self.omitted.add("unsupported_value")
        return f"[omitted: unsupported value {type(value).__name__}]"

    def _mapping(
        self, value: Mapping[object, object], *, depth: int, mode: int
    ) -> PayloadValue:
        out: dict[str, PayloadValue] = {}
        for index, (raw_key, item) in enumerate(value.items()):
            if index >= MAX_ITEMS:
                self.truncated = True
                out["[truncated]"] = f"{len(value) - index} more entries"
                break
            if not isinstance(raw_key, str):
                self.omitted.add("non_string_key")
                continue
            key, _ = sanitize_text(raw_key[:120])
            if _secret_key(raw_key):
                self.redactions += 1
                out[key] = SECRET_MASK
                continue
            if raw_key.lower() in _IDENTITY_KEYS and item is not None:
                self.redactions += 1
                out[key] = MASK
                continue
            out[key] = self.walk(item, depth=depth + 1, mode=_child_mode(raw_key, mode))
        return out

    def _sequence(
        self, value: Sequence[object], *, depth: int, mode: int
    ) -> PayloadValue:
        out: list[PayloadValue] = []
        for index, item in enumerate(value):
            if index >= MAX_ITEMS or self.left <= 0:
                self.truncated = True
                out.append(f"[truncated: {len(value) - index} more items]")
                break
            out.append(self.walk(item, depth=depth + 1, mode=mode))
        return out

    def _string(self, value: str, mode: int) -> str:
        if self.left <= 0:
            self.truncated = True
            return f"[truncated: {len(value)} chars over the payload limit]"
        if mode == _Mode.SQL:
            text, count = sanitize_sql(value)
        elif mode == _Mode.LITERAL:
            text, count = (value, 0) if _safe_literal(value) else (MASK, 1)
        else:
            text, count = sanitize_text(value)
        self.redactions += count
        limit = min(MAX_STRING_CHARS, self.left)
        if len(text) > limit:
            self.truncated = True
            cut = len(text) - limit
            text = f"{text[:limit]} [truncated: {cut} more chars]"
        self.left -= len(text)
        return text
