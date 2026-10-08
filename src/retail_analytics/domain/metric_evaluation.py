"""Reference semantics for metrics over in-memory permitted items.

This pins down edge-case outcomes (empty populations, null amounts, zero
denominators, unknown statuses, window bounds) so fixtures, evidence and the SQL
compiler agree on them. It never decides access: callers pass only items already
restricted to the permitted product scope by trusted code.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from retail_analytics.domain.currency import SourceCurrency
from retail_analytics.domain.metrics import (
    MetricCatalog,
    MetricDefinition,
    MetricDefinitionError,
    MetricStatus,
    Operation,
    Unit,
)
from retail_analytics.domain.periods import DateWindow

MetricValue = Decimal | int


@dataclass(frozen=True, slots=True)
class SalesItem:
    """One permitted item as exposed by the ``sales_items`` logical relation."""

    item_ref: str
    order_ref: str
    customer_ref: str
    product_id: str
    item_status: str | None
    ordered_date: date
    sale_amount: Decimal | None


class Outcome(StrEnum):
    VALUE = "value"
    # No qualifying items: counts and sums are a genuine zero.
    EMPTY_POPULATION = "empty_population"
    # Ratio whose denominator is zero: no value, never zero.
    UNDEFINED_ZERO_DENOMINATOR = "undefined_zero_denominator"
    # Qualifying items exist but none has an amount: unknown, never zero.
    UNDEFINED_ALL_AMOUNTS_MISSING = "undefined_all_amounts_missing"


@dataclass(frozen=True, slots=True)
class MetricResult:
    metric_id: str
    version: int
    status: MetricStatus
    value: MetricValue | None
    outcome: Outcome
    unit: Unit
    currency: SourceCurrency
    window: DateWindow
    qualifying_items: int
    missing_amount_items: int
    unrecognised_status_items: int
    explanation: str

    @property
    def is_exploratory(self) -> bool:
        return self.status is MetricStatus.EXPLORATORY

    def warnings(self) -> tuple[str, ...]:
        out: list[str] = []
        if self.is_exploratory:
            out.append("Exploratory definition: not an approved catalog metric.")
        if self.missing_amount_items:
            out.append(
                f"{self.missing_amount_items} qualifying item(s) have no sale "
                "amount and are excluded from the amount, not counted as zero."
            )
        if self.unit is Unit.CURRENCY_AMOUNT and not self.currency.is_known:
            out.append("Source currency is not verified; no symbol is shown.")
        return tuple(out)


def _in_population(
    definition: MetricDefinition, items: Iterable[SalesItem], window: DateWindow
) -> list[SalesItem]:
    return [
        i
        for i in items
        if window.contains(i.ordered_date)
        and definition.population.qualifies(i.item_status)
    ]


def evaluate_metric(
    catalog: MetricCatalog,
    definition: MetricDefinition,
    items: Iterable[SalesItem],
    window: DateWindow,
    currency: SourceCurrency | None = None,
) -> MetricResult:
    """Compute ``definition`` over ``items`` ordered inside ``window``.

    Exploratory definitions may be evaluated (they are not in ``catalog``) and
    are labelled in the result.
    """
    cur = currency or SourceCurrency.unknown()
    pool = list(items)
    population = _in_population(definition, pool, window)
    missing = sum(1 for i in population if i.sale_amount is None)
    in_window = [i for i in pool if window.contains(i.ordered_date)]
    unrecognised = sum(
        1 for i in in_window if not definition.population.qualifies(i.item_status)
    )

    value: MetricValue | None
    outcome = Outcome.VALUE
    note = ""
    if definition.operation is Operation.RATIO:
        if not (definition.numerator_id and definition.denominator_id):
            raise MetricDefinitionError("ratio without components")
        num = evaluate_metric(
            catalog,
            _ratio_part(catalog, definition, definition.numerator_id),
            pool,
            window,
            cur,
        )
        den = evaluate_metric(
            catalog,
            _ratio_part(catalog, definition, definition.denominator_id),
            pool,
            window,
            cur,
        )
        if den.value is None or den.value == 0:
            value, outcome = None, Outcome.UNDEFINED_ZERO_DENOMINATOR
            note = "Undefined: no qualifying denominator in this window."
        elif num.value is None:
            value, outcome = None, num.outcome
            note = "Undefined: the numerator has no recorded amounts."
        else:
            value = Decimal(num.value) / Decimal(den.value)
        missing = num.missing_amount_items
    elif definition.operation is Operation.SUM:
        amounts = [i.sale_amount for i in population if i.sale_amount is not None]
        if not population:
            value, outcome = Decimal(0), Outcome.EMPTY_POPULATION
        elif not amounts:
            value, outcome = None, Outcome.UNDEFINED_ALL_AMOUNTS_MISSING
            note = "Undefined: qualifying items exist but none has an amount."
        else:
            value = sum(amounts, Decimal(0))
    elif definition.operation is Operation.COUNT_ROWS:
        value = len(population)
    else:
        field_name = definition.distinct_field
        if field_name is None:
            raise MetricDefinitionError("count_distinct without a field")
        value = len({getattr(i, field_name) for i in population})
    if not population and outcome is Outcome.VALUE and value == 0:
        outcome = Outcome.EMPTY_POPULATION
    if outcome is Outcome.EMPTY_POPULATION:
        note = "No qualifying items in this window."
    return MetricResult(
        metric_id=definition.metric_id,
        version=definition.version,
        status=definition.status,
        value=value,
        outcome=outcome,
        unit=definition.unit,
        currency=cur,
        window=window,
        qualifying_items=len(population),
        missing_amount_items=missing,
        unrecognised_status_items=unrecognised,
        explanation=note,
    )


def _ratio_part(
    catalog: MetricCatalog, ratio: MetricDefinition, part_id: str
) -> MetricDefinition:
    """Resolve a ratio component; exploratory ratios reuse their own population."""
    base = catalog.get(part_id)
    if ratio.population == base.population:
        return base
    return MetricDefinition(
        metric_id=base.metric_id,
        version=base.version,
        description=base.description,
        relation=base.relation,
        grain=base.grain,
        operation=base.operation,
        unit=base.unit,
        time_field=base.time_field,
        population=ratio.population,
        measure_field=base.measure_field,
        distinct_field=base.distinct_field,
        supported_dimensions=base.supported_dimensions,
        status=ratio.status,
        rationale=ratio.rationale,
    )


def describe_basis(
    definition: MetricDefinition,
    window: DateWindow,
    currency: SourceCurrency,
    *,
    scope_label: str = "within your permitted products",
) -> str:
    """Calculation basis to disclose beside a figure (contract §6)."""
    label = "Exploratory definition: " if definition.is_exploratory else ""
    money = (
        f" Currency: {currency.label()}."
        if definition.unit is Unit.CURRENCY_AMOUNT
        else ""
    )
    return (
        f"{label}{definition.metric_id} v{definition.version}, calculated from "
        f"items with {definition.population.describe()}, by "
        f"{definition.time_field} between {window.describe()} (UTC), "
        f"{scope_label}.{money}"
    )
