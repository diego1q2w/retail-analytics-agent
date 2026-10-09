from __future__ import annotations

from collections.abc import Mapping
from dataclasses import (
    dataclass,
    field,
)
from datetime import date
from decimal import Decimal
from enum import StrEnum

from retail_analytics.domain.periods import DateWindow

type ScalarValue = str | int | float | bool | date | Decimal


class ParameterType(StrEnum):
    """BigQuery standard SQL parameter types the compiler emits."""

    STRING = "STRING"
    INT64 = "INT64"
    FLOAT64 = "FLOAT64"
    NUMERIC = "NUMERIC"
    BOOL = "BOOL"
    DATE = "DATE"


class DemographicUse(StrEnum):
    """How a compiled query uses customer demographics (compiler-verified).

    Demographics are aggregate-only: a query that would show them for one
    customer, order or item is rejected, so no compiled query is "individual".
    """

    # No demographic field is read anywhere in the query.
    NONE = "none"
    # Demographics reach the result only as group-level statistics.
    AGGREGATE = "aggregate"


@dataclass(frozen=True, slots=True)
class QueryParameter:
    """One named query parameter. ``array`` values are tuples of ``type``."""

    name: str
    type: ParameterType
    value: ScalarValue | tuple[ScalarValue, ...]
    array: bool = False
    # Set by the compiler for authorization parameters; never model-supplied.
    trusted: bool = False
    # Key material: must reach the warehouse job only, never evidence, logs,
    # traces or model context. The value is hidden from ``repr``.
    secret: bool = False

    def __post_init__(self) -> None:
        if self.secret and not self.trusted:
            raise ValueError("secret parameters must be trusted")

    def __repr__(self) -> str:
        shown = "<redacted>" if self.secret else repr(self.value)
        return (
            f"QueryParameter(name={self.name!r}, type={self.type.value}, "
            f"value={shown}, array={self.array}, trusted={self.trusted})"
        )


@dataclass(frozen=True, slots=True)
class AnalysisQuery:
    """Model-authored input: logical SQL and named analysis values (``@name``)."""

    sql: str
    parameters: Mapping[str, ScalarValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FieldRef:
    relation: str
    field: str


@dataclass(frozen=True, slots=True)
class ValueFilter:
    """A comparison of a logical string field with literal values, found by
    the compiler in the model's query (``=``, ``IN``, ``LIKE``; the field may
    be wrapped in ``LOWER``/``UPPER``/``TRIM``).

    ``required`` means the comparison is a top-level ``AND`` condition of a
    ``WHERE`` clause, so the rows that query level reads must match it.
    ``pattern`` means ``values`` are ``LIKE`` patterns.
    """

    field: FieldRef
    values: tuple[str, ...]
    required: bool = False
    pattern: bool = False


@dataclass(frozen=True, slots=True)
class OutputColumn:
    """A result column and the logical fields it is computed from.

    ``direct`` means the value is one logical field passed through unchanged
    (possibly via CTEs/subqueries), which result privacy checks rely on.
    """

    name: str
    sources: frozenset[FieldRef]
    direct: bool


@dataclass(frozen=True, slots=True)
class LatestMonthFilter:
    """The query keeps one calendar month of a year chosen by a scalar subquery.

    Recognized by the compiler only for the documented latest-month shape: the
    top-level query reads one dated relation filtered by
    ``EXTRACT(MONTH FROM d) = <constant>`` and
    ``EXTRACT(YEAR FROM d) = (SELECT MAX(EXTRACT(YEAR FROM ...)) ...)``. The
    year is known only after execution: ``witnesses`` are the output columns
    that carry the filtered rows' own date (the date itself, or its MIN/MAX),
    so the released result states which year the filter selected.
    """

    month: int
    witnesses: tuple[str, ...]

    def __post_init__(self) -> None:
        if not 1 <= self.month <= 12 or not self.witnesses:
            raise ValueError("a latest-month filter needs a month and a witness")


@dataclass(frozen=True, slots=True)
class CompiledQuery:
    """A checked, scope-bound statement. Treat ``sql`` as protected provenance."""

    sql: str
    parameters: tuple[QueryParameter, ...]
    # The model's query after normalization and value extraction (for evidence).
    logical_sql: str
    relations: frozenset[str]
    fields: frozenset[FieldRef]
    outputs: tuple[OutputColumn, ...]
    catalog_version: int
    entitlement_version: int
    maximum_bytes_billed: int
    # The one calendar window every dated read is filtered to, derived by the
    # compiler from the query itself; None when there is none or several.
    date_window: DateWindow | None = None
    # Set instead of ``date_window`` when the year is selected by a subquery;
    # the window is then resolved from the executed result, or stays unknown.
    latest_month: LatestMonthFilter | None = None
    # Verified by the compiler's grain check; the result boundary withholds
    # demographic results unless this says AGGREGATE.
    demographic_use: DemographicUse = DemographicUse.NONE
    # String-field comparisons with literal values (for scope messages; never
    # an authorization input: the bound product scope is the boundary).
    value_filters: tuple[ValueFilter, ...] = ()

    @property
    def analysis_parameters(self) -> tuple[QueryParameter, ...]:
        return tuple(p for p in self.parameters if not p.trusted)

    def __repr__(self) -> str:
        # Never render executed SQL or parameter values in logs by accident.
        return (
            f"CompiledQuery(relations={sorted(self.relations)}, "
            f"outputs={[o.name for o in self.outputs]}, "
            f"catalog_version={self.catalog_version}, "
            f"entitlement_version={self.entitlement_version})"
        )
