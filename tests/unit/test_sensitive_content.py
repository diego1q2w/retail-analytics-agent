"""Person-name screening for shared Golden knowledge and model-bound text."""

from __future__ import annotations

import pytest

from retail_analytics.domain.disclosure import MASK, DisclosureKind
from retail_analytics.domain.sensitive_content import (
    Finding,
    screen_fields,
    screen_for_model,
    screen_text,
)

REF = "cus_" + "0123456789abcdef01234567"


@pytest.mark.parametrize(
    "text",
    [
        "Revenue from the customer named Maria Lopez last quarter",
        "Orders placed by Mrs. Jane Roe",
        "Customer's name: John Smith; compare his spend",
        f"Top buyer Jane Roe ({REF}) bought most",
    ],
)
def test_cued_person_names_are_flagged(text: str) -> None:
    assert Finding.PERSON_NAME in screen_text(text)
    assert ("question", Finding.PERSON_NAME) in screen_fields({"question": text})


@pytest.mark.parametrize(
    "text",
    [
        "Calvin Klein and Tommy Hilfiger led revenue in New York; Levi's grew.",
        "How does spending differ across customer age bands?",
        "Which customers in Texas spent the most? Rank by state.",
        "SELECT month, SUM(sale_price) FROM orders WHERE status = 'Complete'",
    ],
)
def test_brands_places_and_method_text_are_not_names(text: str) -> None:
    assert Finding.PERSON_NAME not in screen_text(text)


def test_cue_less_names_are_a_documented_limitation() -> None:
    # Names have no general shape; without a cue this is left to the reviewer.
    assert Finding.PERSON_NAME not in screen_text("Maria Lopez bought the most")


def test_model_screen_masks_names_and_unpermitted_references() -> None:
    text, kinds = screen_for_model(f"The customer named Maria Lopez, ref {REF}")
    assert "Maria" not in text and REF not in text
    assert MASK in text
    assert kinds == {DisclosureKind.PERSON_NAME, DisclosureKind.OPAQUE_REFERENCE}
    allowed, kinds = screen_for_model(f"ref {REF}", permitted=frozenset({REF}))
    assert REF in allowed and not kinds


def test_model_screen_leaves_clean_text_unchanged() -> None:
    clean = "Sum completed item sales by customer state and rank the states."
    assert screen_for_model(clean) == (clean, frozenset())
