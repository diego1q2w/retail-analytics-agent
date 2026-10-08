"""Port and result types of the restricted analytical SQL compiler.

The model never submits SQL for execution. It submits analytical SQL over the
logical relations of a :class:`CatalogView`; a ``QueryCompiler`` checks it
against an explicit grammar and the view, then binds every logical relation to
a trusted, product-scoped projection. Only the compiled result reaches the
warehouse.

Everything here is SDK-free: the compiled statement is BigQuery SQL text plus
typed parameters, ready for an executor adapter. Authority (catalog view and
product scope) is passed separately from the model-authored query and is never
read from it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from retail_analytics.domain.access import ProductScope
from retail_analytics.domain.catalog import CatalogView
from retail_analytics.domain.operations import ToolErrorCode

# Operational default (1 GiB per query); the executor must apply it to the job.
DEFAULT_MAXIMUM_BYTES_BILLED = 1024**3

# Parameter names with these prefixes belong to the compiler; analysis values
# and SQL identifiers may not use them.
RESERVED_PREFIXES = ("_policy_", "_value_")

type ScalarValue = str | int | float | bool | date | Decimal


class ParameterType(StrEnum):
    """BigQuery standard SQL parameter types the compiler emits."""

    STRING = "STRING"
    INT64 = "INT64"
    FLOAT64 = "FLOAT64"
    NUMERIC = "NUMERIC"
    BOOL = "BOOL"
    DATE = "DATE"


@dataclass(frozen=True, slots=True)
class QueryParameter:
    """One named query parameter. ``array`` values are tuples of ``type``."""

    name: str
    type: ParameterType
    value: ScalarValue | tuple[ScalarValue, ...]
    array: bool = False
    # Set by the compiler for authorization parameters; never model-supplied.
    trusted: bool = False


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
class OutputColumn:
    """A result column and the logical fields it is computed from.

    ``direct`` means the value is one logical field passed through unchanged
    (possibly via CTEs/subqueries), which result privacy checks rely on.
    """

    name: str
    sources: frozenset[FieldRef]
    direct: bool


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

    @property
    def analysis_parameters(self) -> tuple[QueryParameter, ...]:
        return tuple(p for p in self.parameters if not p.trusted)


class QueryRejected(Exception):
    """The query cannot be compiled. Safe to show to the model.

    ``correctable`` tells the agent whether reformulating within the supported
    subset can succeed. Unknown and forbidden names are deliberately reported
    alike so diagnostics never reveal unpublished schema.
    """

    def __init__(
        self,
        code: ToolErrorCode,
        reason: str,
        message: str,
        *,
        relation: str | None = None,
        field: str | None = None,
        available_fields: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.reason = reason
        self.message = message
        self.relation = relation
        self.field = field
        self.available_fields = available_fields

    @property
    def correctable(self) -> bool:
        return self.code in (
            ToolErrorCode.INVALID_QUERY,
            ToolErrorCode.UNSUPPORTED_SQL,
            ToolErrorCode.INVALID_INPUT,
            ToolErrorCode.FIELD_UNAVAILABLE,
        )

    def __repr__(self) -> str:
        return f"QueryRejected({self.code.value}, {self.reason!r})"


class QueryCompiler(Protocol):
    """Compiles model SQL against one executive's current authority.

    ``catalog`` and ``scope`` must be resolved freshly for the attempt by
    trusted code. Raises :class:`QueryRejected`; never returns a statement that
    reads an unbound or unscoped source.
    """

    def compile(
        self, query: AnalysisQuery, *, catalog: CatalogView, scope: ProductScope
    ) -> CompiledQuery: ...
