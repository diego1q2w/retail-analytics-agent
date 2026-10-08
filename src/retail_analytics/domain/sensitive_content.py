"""Screening for content that must never be stored as shared knowledge.

Golden examples are reusable across executives, so their question, SQL, method
summary and report must carry no direct personal data, raw customer or
identifier lists, or result values. Detectors here are a conservative
supplement to human review, not proof of anonymity: they report *which field*
failed and *which kind* of finding, never the matched text, so a rejection
cannot itself leak the content.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from enum import StrEnum


class Finding(StrEnum):
    EMAIL = "email"
    PHONE = "phone"
    ADDRESS = "address"
    LONG_NUMBER = "long_number"
    ID_LIST = "id_list"
    IDENTITY_FILTER = "identity_filter"


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
    return frozenset(f for f, pattern in _PATTERNS if pattern.search(text))


def screen_fields(fields: Mapping[str, str]) -> tuple[tuple[str, Finding], ...]:
    """(field name, finding) pairs, sorted for deterministic messages."""
    return tuple(
        sorted(
            ((name, f) for name, text in fields.items() for f in screen_text(text)),
            key=lambda pair: (pair[0], pair[1].value),
        )
    )
