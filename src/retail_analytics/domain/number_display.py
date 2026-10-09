"""How figures are shown to people: one set of display-precision rules.

Evidence keeps full precision; only displayed text is rounded:

- money totals and averages: two decimals (a nonzero amount too small for
  two decimals keeps three significant digits instead of becoming 0.00);
- percentages: one decimal; shares/ratios at most 1 shown as percentages;
- counts and other whole numbers: no decimals;
- other values: two decimals from 1 upward, three significant digits below.

Missing values are never turned into numbers (callers show "-" or a label).

``round_raw_figures`` is the deterministic release check for generated
text: a written figure with ``RAW_DECIMALS`` or more decimals is a raw,
unrounded value (for example a float copied from evidence) and is rewritten
at display precision, using the matching evidence value's kind when one is
known. Figures with fewer decimals are left as written (the model's own
rounding, or precision the user asked for), as are conversion rates and
anything else the caller protects. IDs and dates are not decimals and are
never touched.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from retail_analytics.domain.disclosure import figures, numeric_value
from retail_analytics.domain.evidence import Evidence
from retail_analytics.domain.metrics import (
    MetricCatalog,
    Operation,
    Unit,
    UnknownMetricError,
)

MONEY_DECIMALS = 2
PERCENT_DECIMALS = 1
SIGNIFICANT_DIGITS = 3
# Written decimals from which a figure counts as a raw, unrounded value.
RAW_DECIMALS = 5
PERCENT_SUFFIXES = ("_pct", "_percent", "_percentage")
SHARE_WORDS = ("share", "rate", "ratio")

# The display rule as the model is told it (instructions use this text).
DISPLAY_RULE = (
    "Show figures rounded for reading: money totals and averages with two "
    "decimals, percentages with one decimal, counts as whole numbers "
    "(unless the user asks for other precision); keep "
    "conversion rates as reported. Never show raw unrounded values or an "
    "'exact figure' with many decimals. A missing value is unavailable, "
    "not 0."
)


@dataclass(frozen=True, slots=True)
class KnownValue:
    """An evidence value and how it is displayed."""

    value: Decimal
    money: bool = False
    percent: bool = False


def is_percent_column(name: str) -> bool:
    folded = name.casefold()
    return folded.endswith(PERCENT_SUFFIXES) or folded.startswith(("pct_", "percent_"))


def is_share_column(name: str) -> bool:
    return any(word in name.casefold().split("_") for word in SHARE_WORDS)


def display_decimals(
    value: Decimal, *, money: bool = False, percent: bool = False
) -> int:
    if percent:
        return PERCENT_DECIMALS
    if money:
        if value and abs(value) < Decimal("0.005"):
            return _significant_decimals(value)
        return MONEY_DECIMALS
    if value == value.to_integral_value():
        return 0
    if abs(value) >= 1:
        return MONEY_DECIMALS
    return _significant_decimals(value)


def display_number(
    value: Decimal, *, money: bool = False, percent: bool = False
) -> str:
    """``value`` at display precision, digits grouped (no unit)."""
    decimals = display_decimals(value, money=money, percent=percent)
    quantum = Decimal(1).scaleb(-decimals)
    try:
        shown = value.quantize(quantum, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return str(value)
    if shown == 0:
        shown = abs(shown)  # never "-0.00"
    text = f"{shown:,.{decimals}f}"
    if (
        not percent
        and decimals
        and abs(value) < 1
        and (not money or decimals > MONEY_DECIMALS)
    ):
        # Significant digits, not a fixed scale: no trailing zeros.
        text = text.rstrip("0").rstrip(".")
    return text


def round_raw_figures(
    text: str,
    known: Sequence[KnownValue] = (),
    *,
    protected: Collection[Decimal] = (),
) -> str:
    """``text`` with raw unrounded figures shown at display precision."""
    out: list[str] = []
    last = 0
    for figure in figures(text):
        if figure.scale != 1 or figure.decimals < RAW_DECIMALS:
            continue
        if any(figure.matches(v) for v in protected):
            continue
        match = next((k for k in known if figure.matches(k.value)), None)
        followed_by_percent = text[figure.end : figure.end + 1] == "%"
        if match is None:
            shown = display_number(figure.value, percent=followed_by_percent)
        else:
            shown = display_number(
                match.value,
                money=match.money,
                percent=match.percent or followed_by_percent,
            )
        out.append(text[last : figure.start])
        out.append(shown)
        last = figure.end
    if not out:
        return text
    out.append(text[last:])
    return "".join(out)


def rounded_raw_cell(cell: object, column_name: str) -> str | None:
    """Display text for a raw unrounded numeric cell (``RAW_DECIMALS`` or more
    decimals), or None to show the cell as it is."""
    if not isinstance(cell, float | Decimal):
        return None
    value = numeric_value(cell)
    if value is None:
        return None
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int) or -exponent < RAW_DECIMALS:
        return None
    return display_number(value, percent=is_percent_column(column_name))


def money_columns(evidence: Evidence, metrics: MetricCatalog) -> frozenset[str]:
    """Value columns read from the measure of a recorded currency definition."""
    analysis = evidence.content.analysis
    if not analysis.definitions_recorded:
        return frozenset()
    measures: set[str] = set()
    for ref in analysis.definitions:
        try:
            definition = metrics.get(ref.metric_id, ref.version)
        except UnknownMetricError:
            continue
        if definition.unit is not Unit.CURRENCY_AMOUNT:
            continue
        if definition.operation is Operation.RATIO:
            for part in (definition.numerator_id, definition.denominator_id):
                if part is None:
                    continue
                inner = metrics.get(part)
                if inner.unit is Unit.CURRENCY_AMOUNT and inner.measure_field:
                    measures.add(f"{inner.relation.value}.{inner.measure_field}")
        elif definition.measure_field:
            measures.add(f"{definition.relation.value}.{definition.measure_field}")
    return frozenset(
        c.name
        for c in evidence.content.table.columns
        if c.role == "value" and measures.intersection(c.sources)
    )


def known_values(
    records: Sequence[Evidence], metrics: MetricCatalog
) -> tuple[tuple[KnownValue, ...], frozenset[Decimal]]:
    """Numeric evidence values with their display kind, and the protected
    values (conversion rates) that are never rewritten."""
    known: list[KnownValue] = []
    protected: set[Decimal] = set()
    for record in records:
        table = record.content.table
        money = money_columns(record, metrics)
        for key, note in record.content.provenance.notes:
            if key == "rate":
                rate = _decimal(note)
                if rate is not None:
                    protected.add(rate)
        for row in table.rows:
            for cell, column in zip(row, table.columns, strict=True):
                if column.role != "value":
                    continue
                value = numeric_value(cell)
                if value is None:
                    continue
                known.append(
                    KnownValue(
                        value,
                        money=column.name in money,
                        percent=is_percent_column(column.name),
                    )
                )
    return tuple(known), frozenset(protected)


def _decimal(text: str) -> Decimal | None:
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def _significant_decimals(value: Decimal) -> int:
    exponent = value.copy_abs().adjusted()  # 0.0042 -> -3
    return max(0, SIGNIFICANT_DIGITS - 1 - exponent)
