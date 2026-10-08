from __future__ import annotations

from collections.abc import Mapping
from dataclasses import (
    dataclass,
    field,
)
from datetime import date
from decimal import Decimal
from enum import StrEnum

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

    def __repr__(self) -> str:
        # Never render executed SQL or parameter values in logs by accident.
        return (
            f"CompiledQuery(relations={sorted(self.relations)}, "
            f"outputs={[o.name for o in self.outputs]}, "
            f"catalog_version={self.catalog_version}, "
            f"entitlement_version={self.entitlement_version})"
        )
