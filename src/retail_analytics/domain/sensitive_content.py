"""Screening for content that must never be stored as shared knowledge.

Golden examples are reusable across executives, so their question, SQL, method
summary and report must carry no direct personal data, raw customer or
identifier lists, or result values. Detectors here are a conservative
supplement to human review, not proof of anonymity: they report *which field*
failed and *which kind* of finding, never the matched text, so a rejection
cannot itself leak the content.

Person names reuse the cue-based detector of ``domain.disclosure`` ("customer
named ...", honorifics, a name next to a customer reference), the same one
that screens model context and output. A bare name with no cue ("Maria Lopez
bought the most") is not detected: names have no general shape, so that stays
with the human reviewer.

``screen_for_model`` is the context screen applied to any text entering the
model (conversation history, evidence notes, retrieved Golden examples): it
masks personal data and opaque references the caller did not permit.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from enum import StrEnum

from retail_analytics.domain.disclosure import (
    Detection,
    DisclosureKind,
    ProtectedTerm,
    mask,
    normalize,
    references,
    scan,
)


class Finding(StrEnum):
    EMAIL = "email"
    PHONE = "phone"
    ADDRESS = "address"
    LONG_NUMBER = "long_number"
    ID_LIST = "id_list"
    IDENTITY_FILTER = "identity_filter"
    PERSON_NAME = "person_name"


_PATTERNS: tuple[tuple[Finding, re.Pattern[str]], ...] = (
    (Finding.EMAIL, re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
    (
        Finding.PHONE,
        re.compile(r"\+\d[\d\s().-]{8,}\d|\(?\b\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b"),
    ),
    (
        Finding.ADDRESS,
        re.compile(
            r"\b\d{1,5}\s+[A-Za-z][\w.]*(?:\s+[A-Za-z][\w.]*){0,3}\s+"
            r"(?:street|st|avenue|ave|road|rd|lane|ln|boulevard|blvd|drive|dr)\b",
            re.IGNORECASE,
        ),
    ),
    # Five or more digits that are not part of a date, time or decimal. Raw
    # customer IDs, product ID lists and result figures all look like this.
    (Finding.LONG_NUMBER, re.compile(r"(?<![\d.\-:/,])\d{5,}(?![\d]|[.:/-]\d)")),
    (Finding.ID_LIST, re.compile(r"\bIN\s*\(\s*\d+(?:\s*,\s*\d+){2,}", re.IGNORECASE)),
    (
        Finding.IDENTITY_FILTER,
        re.compile(
            r"\b(?:user_id|customer_id|email|first_name|last_name|street_address)"
            r"\s*(?:=|<>|!=|IN|LIKE)\s*(?:\(\s*)?['\"\d]",
            re.IGNORECASE,
        ),
    ),
)


def screen_text(text: str) -> frozenset[Finding]:
    found = {f for f, pattern in _PATTERNS if pattern.search(text)}
    if _names_a_person(text):
        found.add(Finding.PERSON_NAME)
    return frozenset(found)


def _names_a_person(text: str) -> bool:
    return any(d.kind is DisclosureKind.PERSON_NAME for d in scan(normalize(text)))


def screen_for_model(
    raw: str,
    *,
    permitted: frozenset[str] = frozenset(),
    protected: Iterable[ProtectedTerm] = (),
) -> tuple[str, frozenset[DisclosureKind]]:
    """``raw`` normalized with personal data and unpermitted references masked.

    Returns the screened text and the kinds that were masked (empty when the
    text was clean). No reference is permitted by default, which is right for
    shared text such as Golden examples.
    """
    text = normalize(raw)
    detections = list(scan(text, protected))
    detections.extend(
        Detection(m.start, m.end, DisclosureKind.OPAQUE_REFERENCE)
        for m in references(text)
        if m.reference not in permitted
    )
    if not detections:
        return text, frozenset()
    return mask(text, detections), frozenset(d.kind for d in detections)


def screen_fields(fields: Mapping[str, str]) -> tuple[tuple[str, Finding], ...]:
    """(field name, finding) pairs, sorted for deterministic messages."""
    return tuple(
        sorted(
            ((name, f) for name, text in fields.items() for f in screen_text(text)),
            key=lambda pair: (pair[0], pair[1].value),
        )
    )
