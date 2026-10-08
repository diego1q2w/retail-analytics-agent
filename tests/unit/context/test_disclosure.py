"""Direct-identifier detection in free text, including encoded smuggling."""

from __future__ import annotations

import base64
from decimal import Decimal

import pytest

from retail_analytics.application.output_privacy import (
    redact_for_telemetry,
    screen_for_memory,
)
from retail_analytics.domain.disclosure import (
    MASK,
    DisclosureKind,
    ProtectedTerm,
    figures,
    mask,
    normalize,
    references,
    scan,
)

EMAIL = "carol@example.invalid"
REF = "cus_0123456789abcdef01234567"


def kinds(text: str, *terms: ProtectedTerm) -> set[DisclosureKind]:
    return {d.kind for d in scan(normalize(text), terms)}


def masked(text: str, *terms: ProtectedTerm) -> str:
    normalized = normalize(text)
    return mask(normalized, scan(normalized, terms))


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        (f"Contact {EMAIL} today", DisclosureKind.EMAIL),
        ("carol at example dot invalid", DisclosureKind.EMAIL),
        ("carol [at] example [dot] invalid", DisclosureKind.EMAIL),
        ("c a r o l @ e x a m p l e . i n v a l i d", DisclosureKind.EMAIL),
        ("Call +1 (555) 123-4567", DisclosureKind.PHONE),
        ("phone: 555 123 4567", DisclosureKind.PHONE),
        ("mobile 5 5 5 1 2 3 4 5 6 7", DisclosureKind.PHONE),
        ("ships to 3 Main St", DisclosureKind.STREET_ADDRESS),
        ("zip code 94107", DisclosureKind.POSTAL_CODE),
        ("located at 37.7749, -122.4194", DisclosureKind.COORDINATES),
        ("She is 37 years old", DisclosureKind.EXACT_AGE),
        ("a 41-year-old buyer", DisclosureKind.EXACT_AGE),
        ("customer aged 29.", DisclosureKind.EXACT_AGE),
        ("age: 93", DisclosureKind.EXACT_AGE),
        ("born in 1985", DisclosureKind.BIRTH_DATE),
        ("DOB 1985-04-02", DisclosureKind.BIRTH_DATE),
        ("customer id 10", DisclosureKind.RAW_IDENTIFIER),
        ("user_id = 20", DisclosureKind.RAW_IDENTIFIER),
        ("order #104 was returned", DisclosureKind.RAW_IDENTIFIER),
        ("customer 12345 bought twice", DisclosureKind.RAW_IDENTIFIER),
        ("The customer named Carol Private spent most", DisclosureKind.PERSON_NAME),
        ("Ms Carol Private", DisclosureKind.PERSON_NAME),
        ("customer's name is Dave Secret", DisclosureKind.PERSON_NAME),
        (f"Carol Private ({REF})", DisclosureKind.PERSON_NAME),
        ("cus_ZZZ3456789abcdef0123456", DisclosureKind.MALFORMED_REFERENCE),
        ("CUS_0123456789ABCDEF01234567", DisclosureKind.MALFORMED_REFERENCE),
        ("bound as _policy_ref_inner", DisclosureKind.INTERNAL_SECRET),
    ],
)
def test_direct_identifiers_are_detected(text: str, kind: DisclosureKind) -> None:
    assert kind in kinds(text)
    assert MASK in masked(text)


@pytest.mark.parametrize(
    "encoded",
    [
        "carol＠example.invalid",  # noqa: RUF001 - full-width at sign on purpose
        "car​ol@exam‍ple.invalid",
        "carol%40example.invalid",
        "carol&#64;example.invalid",
        base64.b64encode(EMAIL.encode()).decode(),
        base64.urlsafe_b64encode(b"customer id 10").decode(),
        EMAIL.encode().hex(),
        base64.b64encode(base64.b64encode(EMAIL.encode())).decode(),
    ],
)
def test_encoded_identifiers_are_detected(encoded: str) -> None:
    text = f"Note: {encoded} is the top buyer"
    assert kinds(text)
    out = masked(text)
    assert EMAIL not in out and encoded not in out
    assert out.startswith("Note: ")


@pytest.mark.parametrize(
    "text",
    [
        "Revenue was $1,234.56 in September 2026, up 12% on August.",
        "Customers aged 35-39 and 90+ in New York (US) bought most.",
        "ages 35 to 39 spent 1.2M; age band 25-29 grew",
        f"{REF} in band 25-29 from Texas spent 105.00",
        "product name: Calvin Klein Jeans led the Women department",
        "Order volume rose to 2,345 orders; average order value 48.20",
        "Compare 2026-09-01 to 2026-09-30 inclusive; 12 months trend",
        "evidence evd_3f2a9c1b0d4e4f5a8b7c6d5e4f3a2b1c v2 supports this",
        "New York (" + REF + ") is the largest state segment",
    ],
)
def test_demographics_figures_and_references_are_not_personal_data(text: str) -> None:
    assert kinds(text) == set()
    assert masked(text) == normalize(text)


def test_protected_terms_match_whole_words_case_insensitively() -> None:
    term = ProtectedTerm("Carol Private")
    assert kinds("top buyer: CAROL   private", term) == {DisclosureKind.PERSON_NAME}
    assert kinds("Carolina Privateer", term) == set()
    with pytest.raises(ValueError, match="three characters"):
        ProtectedTerm("Al")
    assert "Carol" not in repr(term)


def test_references_and_figures_are_parsed_with_precision() -> None:
    text = f"{REF} spent $1,234.57 (1.2M total, 45 orders, 2026)"
    assert [r.reference for r in references(text)] == [REF]
    found = {f.value: f for f in figures(text)}
    spent = found[Decimal("1234.57")]
    assert spent.matches(Decimal("1234.5678")) and not spent.is_trivial
    total = found[Decimal("1200000.0")]
    assert total.matches(Decimal("1234567.89")) and not total.matches(Decimal("900000"))
    assert found[Decimal("45")].is_trivial and found[Decimal("2026")].is_trivial


def test_telemetry_redaction_and_memory_screen() -> None:
    text = f"user asked about {EMAIL} and {REF}; revenue 1,234.50"
    redacted = redact_for_telemetry(text)
    assert EMAIL not in redacted and REF not in redacted
    assert "1,234.50" in redacted
    assert set(screen_for_memory(text)) == {
        DisclosureKind.EMAIL,
        DisclosureKind.OPAQUE_REFERENCE,
    }
    assert screen_for_memory("Revenue means completed item sales") == ()
