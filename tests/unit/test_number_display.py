"""Display precision and the deterministic raw-figure release check."""

from __future__ import annotations

from decimal import Decimal

import pytest

from retail_analytics.application.investigation_policy import (
    render_investigation_policy,
)
from retail_analytics.domain.number_display import (
    DISPLAY_RULE,
    KnownValue,
    display_number,
    round_raw_figures,
)

D = Decimal


@pytest.mark.parametrize(
    ("value", "kind", "shown"),
    [
        (D("1336.5899896621704"), "money", "1,336.59"),
        (D("-12.345"), "money", "-12.35"),
        (D("0"), "money", "0.00"),
        (D("0.004"), "money", "0.004"),
        (D("0.5"), "money", "0.50"),
        (D("-0.0001"), "money", "-0.0001"),
        (D("12.3456"), "percent", "12.3"),
        (D("42"), "count", "42"),
        (D("1234"), "count", "1,234"),
        (D("0.4567"), "other", "0.457"),
        (D("0.5"), "other", "0.5"),
        (D("2.71828"), "other", "2.72"),
    ],
)
def test_display_rules(value: Decimal, kind: str, shown: str) -> None:
    assert (
        display_number(value, money=kind == "money", percent=kind == "percent") == shown
    )


def test_raw_evidence_value_is_rounded_as_money() -> None:
    known = [KnownValue(D("1336.5899896621704"), money=True)]
    text = "Revenue was 1336.59 (exact figure: 1336.5899896621704; 16 items)."
    assert round_raw_figures(text, known) == (
        "Revenue was 1336.59 (exact figure: 1,336.59; 16 items)."
    )


def test_rounding_is_not_indiscriminate() -> None:
    text = (
        "Order 1234567, 2025-09-30, 16 items, 1336.59, 3.1416 as the user "
        "asked, rate 1.0873456, id ref-ab12cd."
    )
    assert round_raw_figures(text, protected=[D("1.0873456")]) == text


def test_unmatched_raw_values_use_generic_precision() -> None:
    assert round_raw_figures("A share of 0.45671234 and 12.3456789%.") == (
        "A share of 0.457 and 12.3%."
    )
    assert round_raw_figures("Change -0.0000123456.") == "Change -0.0000123."


def test_policy_states_display_and_scope_rules() -> None:
    policy = render_investigation_policy({"execute_analysis"})
    assert DISPLAY_RULE in policy
    assert "outside the user's permitted scope" in policy
    assert "never 0" in policy
    assert "without jokes or humour" in policy
    assert "may be reported as 0" in policy
