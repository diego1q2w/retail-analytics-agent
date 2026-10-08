"""Versioned metric definitions and the approved catalog.

Definitions are domain-level and declarative: which logical relation, which
measure, which population, which time field. They carry no product IDs,
identities or entitlements, so a definition can never grant or widen data access;
scope always comes from trusted request context. There is deliberately no
expression language: operations are a small closed set that code compiles.

Source statuses are provisional (contract §6): ``Complete`` qualifies and every
other value, including null and unknown values, does not. Actual source values
are pending live verification (T34).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from retail_analytics.domain.logical_fields import LOGICAL_FIELDS, LogicalRelation

COMPLETED_ITEM_SALES = "completed_item_sales"
COMPLETED_ITEMS = "completed_items"
COMPLETED_ORDERS = "completed_orders"
PURCHASING_CUSTOMERS = "purchasing_customers"
AVERAGE_ORDER_SALES = "average_order_sales"
SALES_PER_CUSTOMER = "sales_per_customer"

# Status value taken from the contract draft; pending live verification (T34).
COMPLETE_STATUS = "Complete"

REVENUE_TERM = "revenue"
DEFAULT_REVENUE_METRIC = COMPLETED_ITEM_SALES


class MetricStatus(StrEnum):
    APPROVED = "approved"
    EXPLORATORY = "exploratory"


class Operation(StrEnum):
    SUM = "sum"
    COUNT_ROWS = "count_rows"
    COUNT_DISTINCT = "count_distinct"
    RATIO = "ratio"


class Unit(StrEnum):
    CURRENCY_AMOUNT = "currency_amount"
    COUNT = "count"


class MetricDefinitionError(ValueError):
    """A definition is malformed or references something unsupported."""


class UnknownMetricError(KeyError):
    """No definition exists; the message says what is missing."""


@dataclass(frozen=True, slots=True)
class PopulationFilter:
    """Which rows qualify. Null or unlisted statuses never qualify."""

    status_field: str
    qualifying_statuses: frozenset[str]

    def __post_init__(self) -> None:
        if not self.qualifying_statuses:
            raise MetricDefinitionError("a population filter needs a status")

    def qualifies(self, status: str | None) -> bool:
        return status is not None and status in self.qualifying_statuses

    def describe(self) -> str:
        return "status " + " or ".join(sorted(self.qualifying_statuses))


@dataclass(frozen=True, slots=True)
class MetricDefinition:
    """One immutable version of a metric."""

    metric_id: str
    version: int
    description: str
    relation: LogicalRelation
    grain: str
    operation: Operation
    unit: Unit
    time_field: str
    population: PopulationFilter
    measure_field: str | None = None
    distinct_field: str | None = None
    numerator_id: str | None = None
    denominator_id: str | None = None
    supported_dimensions: frozenset[str] = frozenset()
    status: MetricStatus = MetricStatus.APPROVED
    # Exploratory definitions must say who defined them and why.
    rationale: str | None = None

    def __post_init__(self) -> None:
        if not self.metric_id or self.version < 1:
            raise MetricDefinitionError("metric needs an id and version >= 1")
        fields = LOGICAL_FIELDS[self.relation]
        used = [self.time_field, self.population.status_field]
        used += [f for f in (self.measure_field, self.distinct_field) if f]
        used += list(self.supported_dimensions)
        unknown = sorted(f for f in used if f not in fields)
        if unknown:
            raise MetricDefinitionError(
                f"{self.metric_id}: fields not in {self.relation}: {unknown}"
            )
        self._check_operation()
        if self.status is MetricStatus.EXPLORATORY and not self.rationale:
            raise MetricDefinitionError("exploratory definitions need a rationale")

    def _check_operation(self) -> None:
        op = self.operation
        ratio_ids = (self.numerator_id, self.denominator_id)
        if op is Operation.SUM and not self.measure_field:
            raise MetricDefinitionError("sum needs a measure_field")
        if op is Operation.COUNT_DISTINCT and not self.distinct_field:
            raise MetricDefinitionError("count_distinct needs a distinct_field")
        if op is Operation.RATIO and not all(ratio_ids):
            raise MetricDefinitionError("ratio needs numerator and denominator")
        if op is not Operation.RATIO and any(ratio_ids):
            raise MetricDefinitionError("only ratios reference other metrics")
        if op is not Operation.SUM and self.measure_field:
            raise MetricDefinitionError("only sum takes a measure_field")
        if op is not Operation.COUNT_DISTINCT and self.distinct_field:
            raise MetricDefinitionError("only count_distinct takes a distinct_field")

    @property
    def key(self) -> tuple[str, int]:
        return (self.metric_id, self.version)

    @property
    def is_exploratory(self) -> bool:
        return self.status is MetricStatus.EXPLORATORY


def _sales_items_metric(
    metric_id: str,
    description: str,
    grain: str,
    operation: Operation,
    unit: Unit,
    *,
    measure_field: str | None = None,
    distinct_field: str | None = None,
    numerator_id: str | None = None,
    denominator_id: str | None = None,
) -> MetricDefinition:
    return MetricDefinition(
        metric_id=metric_id,
        version=1,
        description=description,
        relation=LogicalRelation.SALES_ITEMS,
        grain=grain,
        operation=operation,
        unit=unit,
        time_field="ordered_date",
        population=PopulationFilter("item_status", frozenset({COMPLETE_STATUS})),
        supported_dimensions=frozenset({"product_id"}),
        measure_field=measure_field,
        distinct_field=distinct_field,
        numerator_id=numerator_id,
        denominator_id=denominator_id,
    )


def builtin_metrics() -> tuple[MetricDefinition, ...]:
    """Version 1 metrics from analytical data contract §6."""
    return (
        _sales_items_metric(
            COMPLETED_ITEM_SALES,
            "Sum of sale_amount over completed items; not net revenue.",
            "item",
            Operation.SUM,
            Unit.CURRENCY_AMOUNT,
            measure_field="sale_amount",
        ),
        _sales_items_metric(
            COMPLETED_ITEMS,
            "Count of completed item rows.",
            "item",
            Operation.COUNT_ROWS,
            Unit.COUNT,
        ),
        _sales_items_metric(
            COMPLETED_ORDERS,
            "Distinct orders with at least one completed permitted item.",
            "order",
            Operation.COUNT_DISTINCT,
            Unit.COUNT,
            distinct_field="order_ref",
        ),
        _sales_items_metric(
            PURCHASING_CUSTOMERS,
            "Distinct customers with a completed permitted purchase.",
            "customer",
            Operation.COUNT_DISTINCT,
            Unit.COUNT,
            distinct_field="customer_ref",
        ),
        _sales_items_metric(
            AVERAGE_ORDER_SALES,
            "Completed-item sales per completed order (partial-basket value).",
            "order",
            Operation.RATIO,
            Unit.CURRENCY_AMOUNT,
            numerator_id=COMPLETED_ITEM_SALES,
            denominator_id=COMPLETED_ORDERS,
        ),
        _sales_items_metric(
            SALES_PER_CUSTOMER,
            "Completed-item sales per purchasing customer within scope.",
            "customer",
            Operation.RATIO,
            Unit.CURRENCY_AMOUNT,
            numerator_id=COMPLETED_ITEM_SALES,
            denominator_id=PURCHASING_CUSTOMERS,
        ),
    )


@dataclass(frozen=True, slots=True)
class MetricCatalog:
    """Reviewed, versioned approved metrics. Not an allowlist of questions."""

    definitions: tuple[MetricDefinition, ...]
    _index: dict[tuple[str, int], MetricDefinition] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        index: dict[tuple[str, int], MetricDefinition] = {}
        for d in self.definitions:
            if d.is_exploratory:
                raise MetricDefinitionError(
                    f"{d.metric_id}: exploratory definitions need review before "
                    "entering the shared catalog"
                )
            if d.key in index:
                raise MetricDefinitionError(f"duplicate metric {d.key}")
            index[d.key] = d
        object.__setattr__(self, "_index", index)
        for d in self.definitions:
            self._check_ratio_refs(d)

    def _check_ratio_refs(self, d: MetricDefinition) -> None:
        if d.operation is not Operation.RATIO:
            return
        for ref in (d.numerator_id, d.denominator_id):
            if ref is None or ref not in self.metric_ids():
                raise MetricDefinitionError(f"{d.metric_id}: unknown metric {ref}")
            target = self.get(ref)
            if target.operation is Operation.RATIO:
                raise MetricDefinitionError(f"{d.metric_id}: ratios of ratios")
            if (target.relation, target.time_field, target.population) != (
                d.relation,
                d.time_field,
                d.population,
            ):
                raise MetricDefinitionError(
                    f"{d.metric_id}: {ref} uses a different population or time field"
                )

    def metric_ids(self) -> frozenset[str]:
        return frozenset(i for i, _ in self._index)

    def latest_version(self, metric_id: str) -> int:
        versions = [v for i, v in self._index if i == metric_id]
        if not versions:
            raise UnknownMetricError(
                f"No approved definition for '{metric_id}'. Churn, retention and "
                "premium products have no default definition: establish the "
                "population, window and thresholds as an exploratory definition."
            )
        return max(versions)

    def get(self, metric_id: str, version: int | None = None) -> MetricDefinition:
        v = self.latest_version(metric_id) if version is None else version
        try:
            return self._index[(metric_id, v)]
        except KeyError:
            raise UnknownMetricError(
                f"{metric_id} version {v} does not exist"
            ) from None

    def default_revenue(self) -> MetricDefinition:
        return self.get(DEFAULT_REVENUE_METRIC)


def default_catalog() -> MetricCatalog:
    return MetricCatalog(builtin_metrics())


def exploratory_definition(
    base: MetricDefinition,
    *,
    metric_id: str,
    rationale: str,
    qualifying_statuses: Iterable[str] | None = None,
) -> MetricDefinition:
    """Derive a labelled exploratory variant, e.g. another status population.

    The variant is computable and carries its definition into evidence, but is
    not in the approved catalog; promotion needs review.
    """
    population = (
        base.population
        if qualifying_statuses is None
        else PopulationFilter(
            base.population.status_field, frozenset(qualifying_statuses)
        )
    )
    return MetricDefinition(
        metric_id=metric_id,
        version=1,
        description=f"Exploratory variant of {base.metric_id}",
        relation=base.relation,
        grain=base.grain,
        operation=base.operation,
        unit=base.unit,
        time_field=base.time_field,
        population=population,
        measure_field=base.measure_field,
        distinct_field=base.distinct_field,
        numerator_id=base.numerator_id,
        denominator_id=base.denominator_id,
        supported_dimensions=base.supported_dimensions,
        status=MetricStatus.EXPLORATORY,
        rationale=rationale,
    )


PERMITTED_PRODUCTS_SCOPE_LABEL = "within your permitted products"


@dataclass(frozen=True, slots=True)
class ChurnDefinition:
    """Exploratory churn: needs an explicit population and inactivity window.

    Absence of purchases in permitted products never establishes company-wide
    churn, so the scope label is fixed and cannot be overridden.
    """

    population: str
    inactivity_days: int
    observation_days: int
    rationale: str

    def __post_init__(self) -> None:
        if not self.population.strip() or not self.rationale.strip():
            raise MetricDefinitionError("churn needs a population and rationale")
        if self.inactivity_days < 1 or self.observation_days < self.inactivity_days:
            raise MetricDefinitionError(
                "observation window must cover the inactivity window"
            )

    @property
    def scope_label(self) -> str:
        return PERMITTED_PRODUCTS_SCOPE_LABEL

    def describe(self) -> str:
        return (
            f"Exploratory churn: {self.population}, no completed purchase for "
            f"{self.inactivity_days} days within a {self.observation_days}-day "
            f"observation window, {self.scope_label}; not company-wide churn."
        )
