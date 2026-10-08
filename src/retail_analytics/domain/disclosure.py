"""Detection of direct personal data in free text leaving or entering the model.

Applies the accepted direct-identifier policy to generated text, user text and
history: names, contact details, identifying addresses/locations, raw customer
or order keys, exact ages and birth dates are masked or blocked; opaque
references, demographics (country, state, age bands) and figures are not
personal data by themselves. Following the accepted policy there is no
minimum group size.

Detectors are deterministic and conservative. Text is first normalized (NFKC,
format characters removed) so look-alike and zero-width tricks collapse, and
tokens that look encoded (percent, HTML entity, base64, hex) are decoded and
scanned again, so an identifier cannot be smuggled in an encoding. Names have
no general shape: they are found by context cues ("customer named ...",
honorifics, a name next to a customer reference) and by exact match against
protected terms supplied by trusted code (for example names the user typed).
This is defense in depth on top of the compiler and result boundary, which
keep identifiers out of model input in the first place; it is not an
anonymity guarantee.

Findings carry kinds and positions only; callers never log matched text.
"""

from __future__ import annotations

import base64
import binascii
import html
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum
from urllib.parse import unquote

from retail_analytics.domain.privacy import REFERENCE_HEX_CHARS, REFERENCE_PREFIXES

MASK = "[withheld]"


class DisclosureKind(StrEnum):
    EMAIL = "email"
    PHONE = "phone"
    STREET_ADDRESS = "street_address"
    POSTAL_CODE = "postal_code"
    COORDINATES = "coordinates"
    EXACT_AGE = "exact_age"
    BIRTH_DATE = "birth_date"
    RAW_IDENTIFIER = "raw_identifier"
    PERSON_NAME = "person_name"
    # An identifier hidden in an encoding (base64, hex, percent, entities).
    ENCODED_IDENTIFIER = "encoded_identifier"
    # Something shaped like an opaque reference that is not a valid one.
    MALFORMED_REFERENCE = "malformed_reference"
    # Trusted query parameters or key material named in text.
    INTERNAL_SECRET = "internal_secret"  # noqa: S105 - a category name
    # A valid opaque reference: allowed in answers when current evidence
    # contains it, never in telemetry or cross-executive memory.
    OPAQUE_REFERENCE = "opaque_reference"


@dataclass(frozen=True, slots=True, order=True)
class Detection:
    start: int
    end: int
    kind: DisclosureKind


@dataclass(frozen=True, slots=True)
class ProtectedTerm:
    """A literal that must never appear in output (e.g. a name the user typed)."""

    text: str
    kind: DisclosureKind = DisclosureKind.PERSON_NAME

    def __post_init__(self) -> None:
        if len(self.text.strip()) < 3:
            raise ValueError("protected terms need at least three characters")

    def __repr__(self) -> str:
        return f"ProtectedTerm(<{len(self.text)} chars>, {self.kind.value})"


_NAME = r"[A-Z][a-z'\u2019\-]+"
_FULL_NAME = rf"{_NAME}(?:\s+{_NAME}){{1,2}}"
_FLAGS = re.IGNORECASE

_EMAIL = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")
_OBFUSCATED_EMAIL = re.compile(
    r"\b[\w.+\-]+\s*(?:\(at\)|\[at\]|\{at\}|<at>)\s*[A-Za-z][\w\-]*"
    r"(?:\s*(?:\(dot\)|\[dot\]|\{dot\}|<dot>|\sdot\s|\.)\s*[A-Za-z][\w\-]*)+"
    # A bare " at " needs a spelled-out " dot " to avoid ordinary prose.
    r"|\b[\w.+\-]+\s+at\s+[A-Za-z][\w\-]*(?:\s+dot\s+[A-Za-z][\w\-]*)+",
    _FLAGS,
)
# Characters spaced out one by one around an "@": "j o h n @ x . c o m".
_SPACED_EMAIL = re.compile(r"(?:[\w.+\-] ){2,}[\w.+\-]? ?@ ?(?:[\w.\-] ?){4,}")
_PHONE = re.compile(
    r"\+\d[\d\s().\-]{8,}\d"
    r"|\(?\b\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}\b"
)
_CUED_PHONE = re.compile(
    r"\b(?:phone|tel|telephone|mobile|cell|call|whatsapp|fax)\b\W{0,12}"
    r"(?:\d[\s().\-]{0,2}){7,}",
    _FLAGS,
)
_STREET = re.compile(
    r"\b\d{1,5}\s+[A-Za-z][\w.]*(?:\s+[A-Za-z][\w.]*){0,3}\s+"
    r"(?:street|st|avenue|ave|road|rd|lane|ln|boulevard|blvd|drive|dr|court|ct|"
    r"place|pl|way|terrace|highway|hwy)\b",
    _FLAGS,
)
_POSTAL = re.compile(
    r"\b(?:zip|postal|post)(?:\s*code)?\b\s*(?:[:#=]|is)?\s*"
    r"[A-Z0-9]*\d[A-Z0-9\- ]{2,8}",
    _FLAGS,
)
_COORDINATES = re.compile(r"-?\b\d{1,2}\.\d{4,}\s*,\s*-?\d{1,3}\.\d{4,}\b")
_EXACT_AGE = re.compile(
    r"\b\d{1,3}\s*(?:-|\s)?\s*(?:years?|yrs?)\s*(?:-|\s)?\s*old\b"
    r"|\b\d{1,3}\s*(?:yo|y/o)\b"
    r"|\b(?:aged?|ages)\s*(?:[:=]|is|of)?\s*\d{1,3}\b"
    r"(?!\s*(?:[\-\u2013\u2014+]|to\b|through\b|and\b|or\b|plus\b|\d))",
    _FLAGS,
)
_BIRTH = re.compile(
    r"\bborn\s+(?:in|on)\s+[\w ,./\-]{0,20}\d{2,4}"
    r"|\b(?:date\s+of\s+birth|birth\s*date|birthday|d\.?o\.?b\.?)\b"
    r"\W{0,4}[\w ,./\-]{0,20}",
    _FLAGS,
)
_KEY_NOUN = r"(?:customer|user|client|buyer|shopper|order|item|purchaser)"
_RAW_IDENTIFIER = re.compile(
    rf"\b{_KEY_NOUN}[\s_\-]*(?:id|ids|number|num|no\.?|key|#)\s*(?:[:#=]|is|was)?\s*"
    r"#?\s*\d+\b"
    rf"|\b{_KEY_NOUN}\s*#\s*\d+\b"
    rf"|\b{_KEY_NOUN}\s+(?!(?:19|20)\d\d\b)\d{{4,}}\b"
    r"|\b(?:id|user_id|customer_id|order_id|item_id)\s*(?:=|==|<>|!=|\bin\b|:)\s*"
    r"\(?\s*['\"]?\d+",
    _FLAGS,
)
_SECRET = re.compile(
    r"\b_policy_\w+|RETAIL_ANALYTICS_REFERENCE_KEY|reference[_ ]master[_ ]key",
    _FLAGS,
)
# Cue words are case-insensitive (scoped flag); the name itself must be
# capitalized, which keeps ordinary lower-case prose out.
_PERSON_CUES = (
    re.compile(r"\b(?:Mr|Mrs|Ms|Miss|Mx)\.?\s+(" + _NAME + r"(?:\s+" + _NAME + r")?)"),
    re.compile(
        r"\b(?i:customer|client|shopper|buyer|user|person|purchaser|recipient|"
        r"cardholder|someone|woman|man)(?i:s)?\s+(?i:named|called|name\s+is|"
        r"name:|by\s+the\s+name\s+of)\s+(" + _NAME + r"(?:\s+" + _NAME + r"){0,2})"
    ),
    re.compile(
        r"\b(?i:full|first|last|given|family|sur|customer'?s?|client'?s?|"
        r"person'?s?)\s*(?i:name)\s*(?i:is|:|=)\s*("
        + _NAME
        + r"(?:\s+"
        + _NAME
        + r"){0,2})"
    ),
    # A full name attached to a customer reference: "Jane Roe (cus_...)".
    re.compile(r"(" + _FULL_NAME + r")\s*[|,:(\[\-\u2013\u2014]+\s*cus_"),
)
# Two-word places that are allowed demographics and look like names.
_PLACE_NAMES = frozenset(
    {
        "new york",
        "new jersey",
        "new mexico",
        "new hampshire",
        "north carolina",
        "south carolina",
        "north dakota",
        "south dakota",
        "west virginia",
        "rhode island",
        "district columbia",
        "united states",
        "united kingdom",
        "south korea",
        "new zealand",
        "south africa",
        "hong kong",
        "costa rica",
        "puerto rico",
    }
)
_PREFIXES = "|".join(sorted(set(REFERENCE_PREFIXES.values())))
_REFERENCE = re.compile(rf"\b(?:{_PREFIXES})_[0-9a-f]{{{REFERENCE_HEX_CHARS}}}\b")
_REFERENCE_LIKE = re.compile(rf"\b(?:{_PREFIXES})_[0-9A-Za-z+/=\-]{{4,}}", _FLAGS)

_TOKEN = re.compile(r"\S+")
_BASE64_TOKEN = re.compile(r"[A-Za-z0-9+/_\-]{12,}={0,2}")
_HEX_TOKEN = re.compile(r"\b(?:[0-9a-fA-F]{2}){6,}\b")
_PERCENT = re.compile(r"%[0-9A-Fa-f]{2}")
_ENTITY = re.compile(r"&#?\w+;")
_MAX_DECODE_DEPTH = 2

_FIGURE = re.compile(
    r"(?<![\w.\-/:])-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:\s?[kKmMbB]\b)?"
    r"(?![\w\-/:]|\.\d)"
)
_MULTIPLIERS = {"k": Decimal(1000), "m": Decimal(10**6), "b": Decimal(10**9)}


def normalize(text: str) -> str:
    """Fold compatibility forms (full-width at signs, ligatures) and drop format chars.

    Removing format characters (zero-width spaces, joiners, bidi controls)
    collapses identifiers split by invisible characters.
    """
    folded = unicodedata.normalize("NFKC", text)
    return "".join(ch for ch in folded if unicodedata.category(ch) != "Cf")


def scan(text: str, protected: Iterable[ProtectedTerm] = ()) -> tuple[Detection, ...]:
    """Personal-data findings in already-normalized ``text``, merged and sorted."""
    found = list(_scan_plain(text))
    found.extend(_scan_encoded(text, depth=0))
    found.extend(_scan_terms(text, protected))
    return _merge(found)


def _scan_plain(text: str) -> list[Detection]:
    found: list[Detection] = []
    simple: tuple[tuple[DisclosureKind, re.Pattern[str]], ...] = (
        (DisclosureKind.EMAIL, _EMAIL),
        (DisclosureKind.EMAIL, _OBFUSCATED_EMAIL),
        (DisclosureKind.PHONE, _PHONE),
        (DisclosureKind.PHONE, _CUED_PHONE),
        (DisclosureKind.STREET_ADDRESS, _STREET),
        (DisclosureKind.POSTAL_CODE, _POSTAL),
        (DisclosureKind.COORDINATES, _COORDINATES),
        (DisclosureKind.EXACT_AGE, _EXACT_AGE),
        (DisclosureKind.BIRTH_DATE, _BIRTH),
        (DisclosureKind.RAW_IDENTIFIER, _RAW_IDENTIFIER),
        (DisclosureKind.INTERNAL_SECRET, _SECRET),
    )
    for kind, pattern in simple:
        found.extend(
            Detection(m.start(), m.end(), kind) for m in pattern.finditer(text)
        )
    for match in _SPACED_EMAIL.finditer(text):
        if _EMAIL.fullmatch(re.sub(r"\s+", "", match.group())):
            found.append(Detection(match.start(), match.end(), DisclosureKind.EMAIL))
    for pattern in _PERSON_CUES:
        for match in pattern.finditer(text):
            name = match.group(1)
            if " ".join(name.lower().split()) in _PLACE_NAMES:
                continue
            found.append(
                Detection(match.start(1), match.end(1), DisclosureKind.PERSON_NAME)
            )
    for match in _REFERENCE_LIKE.finditer(text):
        if not _REFERENCE.fullmatch(match.group()):
            found.append(
                Detection(
                    match.start(), match.end(), DisclosureKind.MALFORMED_REFERENCE
                )
            )
    return found


def _scan_encoded(text: str, *, depth: int) -> list[Detection]:
    """Tokens whose decoded form contains personal data or key material."""
    if depth >= _MAX_DECODE_DEPTH:
        return []
    found: list[Detection] = []
    for token in _TOKEN.finditer(text):
        # Valid references are opaque by design; never decode them.
        word = _REFERENCE.sub(" ", token.group())
        for decoded in _decodings(word):
            inner = normalize(decoded)
            if _scan_plain(inner) or _scan_encoded(inner, depth=depth + 1):
                found.append(
                    Detection(
                        token.start(), token.end(), DisclosureKind.ENCODED_IDENTIFIER
                    )
                )
                break
    return found


def _decodings(word: str) -> list[str]:
    variants: list[str] = []
    if _PERCENT.search(word):
        variants.append(unquote(word))
    if _ENTITY.search(word):
        variants.append(html.unescape(word))
    for match in _BASE64_TOKEN.finditer(word):
        decoded = _base64_text(match.group())
        if decoded is not None:
            variants.append(decoded)
    for match in _HEX_TOKEN.finditer(word):
        decoded = _printable(bytes.fromhex(match.group()))
        if decoded is not None:
            variants.append(decoded)
    return [v for v in variants if v != word]


def _base64_text(token: str) -> str | None:
    padded = token + "=" * (-len(token) % 4)
    for decode in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            raw = decode(padded.encode())
        except (binascii.Error, ValueError):
            continue
        text = _printable(raw)
        if text is not None:
            return text
    return None


def _printable(raw: bytes) -> str | None:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if len(text) < 4:
        return None
    printable = sum(ch.isprintable() for ch in text)
    return text if printable / len(text) >= 0.9 else None


def _scan_terms(text: str, protected: Iterable[ProtectedTerm]) -> list[Detection]:
    found: list[Detection] = []
    for term in protected:
        words = normalize(term.text).split()
        if not words:
            continue
        pattern = re.compile(
            r"(?<!\w)" + r"\s+".join(re.escape(w) for w in words) + r"(?!\w)", _FLAGS
        )
        found.extend(
            Detection(m.start(), m.end(), term.kind) for m in pattern.finditer(text)
        )
    return found


def _merge(found: Iterable[Detection]) -> tuple[Detection, ...]:
    """Overlapping findings become one span carrying the first-found kind."""
    merged: list[Detection] = []
    for item in sorted(found):
        if merged and item.start < merged[-1].end:
            last = merged[-1]
            merged[-1] = Detection(last.start, max(last.end, item.end), last.kind)
        else:
            merged.append(item)
    return tuple(merged)


def mask(text: str, detections: Iterable[Detection], replacement: str = MASK) -> str:
    parts: list[str] = []
    position = 0
    for item in _merge(detections):
        parts.append(text[position : item.start])
        parts.append(replacement)
        position = item.end
    parts.append(text[position:])
    return "".join(parts)


def detected_terms(
    text: str, detections: Iterable[Detection]
) -> tuple[ProtectedTerm, ...]:
    """Literal names/contacts found in ``text`` (e.g. user input) to protect later."""
    kinds = {DisclosureKind.PERSON_NAME, DisclosureKind.EMAIL, DisclosureKind.PHONE}
    terms: dict[str, ProtectedTerm] = {}
    for item in detections:
        literal = text[item.start : item.end].strip()
        if item.kind in kinds and len(literal) >= 3:
            terms.setdefault(literal.lower(), ProtectedTerm(literal, item.kind))
    return tuple(terms.values())


@dataclass(frozen=True, slots=True)
class ReferenceMention:
    reference: str
    start: int
    end: int


def references(text: str) -> tuple[ReferenceMention, ...]:
    """Well-formed opaque references in ``text``."""
    return tuple(
        ReferenceMention(m.group(), m.start(), m.end())
        for m in _REFERENCE.finditer(text)
    )


@dataclass(frozen=True, slots=True)
class Figure:
    """A number as written: ``value`` scaled, ``decimals`` as displayed."""

    value: Decimal
    decimals: int
    scale: Decimal
    start: int
    end: int

    @property
    def is_trivial(self) -> bool:
        """Small integers and years coincide with real figures far too often."""
        if self.decimals or self.scale != 1:
            return False
        magnitude = abs(self.value)
        return magnitude < 1000 or (1900 <= magnitude <= 2100)

    def matches(self, value: Decimal) -> bool:
        """``value`` displayed at this figure's scale and precision equals it."""
        try:
            shown = (value / self.scale).quantize(
                Decimal(1).scaleb(-self.decimals), rounding=ROUND_HALF_UP
            )
        except InvalidOperation:
            return False
        return shown == self.value / self.scale


def figures(text: str) -> tuple[Figure, ...]:
    found: list[Figure] = []
    for match in _FIGURE.finditer(text):
        raw = match.group().replace(",", "").replace(" ", "")
        scale = Decimal(1)
        if raw[-1].lower() in _MULTIPLIERS:
            scale = _MULTIPLIERS[raw[-1].lower()]
            raw = raw[:-1]
        try:
            number = Decimal(raw)
        except InvalidOperation:
            continue
        decimals = len(raw.split(".")[1]) if "." in raw else 0
        found.append(
            Figure(number * scale, decimals, scale, match.start(), match.end())
        )
    return tuple(found)


def numeric_value(cell: object) -> Decimal | None:
    """A numeric cell as Decimal (bools and non-finite floats are not figures)."""
    if isinstance(cell, bool) or not isinstance(cell, int | float | Decimal):
        return None
    try:
        value = Decimal(str(cell)) if isinstance(cell, float) else Decimal(cell)
    except InvalidOperation:
        return None
    return value if value.is_finite() else None
