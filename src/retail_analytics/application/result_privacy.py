"""Privacy boundary for query results: the only way rows leave the executor.

Every warehouse result passes :meth:`ResultPrivacyBoundary.release` before it
reaches the model, evidence, reports or telemetry. The compiler already keeps
forbidden fields out of the query; this boundary independently checks what
actually came back, with the *current* authority, and fails closed:

- authorization and catalog versions still match the compiled query, and
  every output's lineage is a currently published field whose sources are
  allowed (raw keys only as references, exact age only as an age band, no
  direct identifiers);
- the result has exactly the compiled output columns and only plain scalar
  values;
- a column that passes a reference or age band through unchanged holds only
  well-formed references of that kind or grid age bands, so a broken
  derivation cannot leak raw keys or ages;
- free-text values that look like direct personal data (email, phone, street
  address) are masked and counted, as defense in depth;
- rows are bounded (operational default 500 rows / 256 KiB) with truthful
  truncation flags.

Following the accepted policy there is no minimum group size: small and
single-customer groups are released like any other. This is not an anonymity
guarantee.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from retail_analytics.application.query_compiler import CompiledQuery, FieldRef
from retail_analytics.domain.catalog import (
    DIRECT_IDENTIFIER_COLUMNS,
    EXACT_AGE_COLUMNS,
    IDENTIFIER_COLUMNS,
    PRODUCT_KEY_TABLE,
    CatalogView,
    Derivation,
    FieldView,
)
from retail_analytics.domain.operations import ToolErrorCode
from retail_analytics.domain.privacy import is_age_band, is_reference
from retail_analytics.domain.sensitive_content import Finding, screen_text

PRIVACY_POLICY_VERSION = 1
MASK = "[withheld]"
DEFAULT_MAX_ROWS = 500
DEFAULT_MAX_BYTES = 256 * 1024

type Cell = str | int | float | bool | Decimal | date | datetime | None

_MASKED_FINDINGS = frozenset({Finding.EMAIL, Finding.PHONE, Finding.ADDRESS})
_SCALARS = (str, int, float, bool, Decimal, date, datetime)


class ColumnRole(StrEnum):
    # Passes one opaque reference through unchanged.
    REFERENCE = "reference"
    # Passes the trusted age band through unchanged.
    AGE_BAND = "age_band"
    VALUE = "value"


class TruncationReason(StrEnum):
    ROWS = "rows"
    BYTES = "bytes"
    # The executor itself stopped reading before the end of the result.
    SOURCE = "source"


@dataclass(frozen=True, slots=True)
class ResultLimits:
    max_rows: int = DEFAULT_MAX_ROWS
    max_bytes: int = DEFAULT_MAX_BYTES

    def __post_init__(self) -> None:
        if self.max_rows < 1 or self.max_bytes < 1024:
            raise ValueError("result limits are too small")


@dataclass(frozen=True, slots=True)
class QueryRows:
    """What an executor read back, before any privacy check.

    ``complete`` is False when the executor stopped reading early (for example
    at its own page limit); the release is then marked truncated.
    """

    columns: tuple[str, ...]
    rows: Sequence[Sequence[object]]
    complete: bool = True

    def __repr__(self) -> str:
        return f"QueryRows(columns={self.columns}, rows=<{len(self.rows)} rows>)"


@dataclass(frozen=True, slots=True)
class ReleasedColumn:
    name: str
    sources: tuple[FieldRef, ...]
    role: ColumnRole


@dataclass(frozen=True, slots=True)
class ReleasedResult:
    """Rows that passed the boundary; safe for the model and evidence."""

    columns: tuple[ReleasedColumn, ...]
    rows: tuple[tuple[Cell, ...], ...]
    received_rows: int
    truncation: TruncationReason | None
    masked_cells: int
    masked_columns: tuple[str, ...]
    catalog_version: int
    entitlement_version: int
    policy_version: int = PRIVACY_POLICY_VERSION

    @property
    def truncated(self) -> bool:
        return self.truncation is not None

    def records(self) -> list[dict[str, Cell]]:
        names = [c.name for c in self.columns]
        return [dict(zip(names, row, strict=True)) for row in self.rows]

    def __repr__(self) -> str:
        return (
            f"ReleasedResult(columns={[c.name for c in self.columns]}, "
            f"rows=<{len(self.rows)} rows>, truncation={self.truncation})"
        )


class ResultWithheld(Exception):
    """The result cannot be released. ``reason`` is internal; ``message`` safe."""

    def __init__(self, code: ToolErrorCode, reason: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.reason = reason
        self.message = message

    def __repr__(self) -> str:
        return f"ResultWithheld({self.code.value}, {self.reason!r})"


class ResultPrivacyBoundary:
    """Releases query results under the current privacy policy.

    ``catalog`` must be the executive's view resolved freshly for this release
    (not the one used at compile time), so revoked access or a withdrawn field
    withholds results that are already computed.
    """

    def __init__(self, limits: ResultLimits | None = None) -> None:
        self._limits = limits or ResultLimits()

    def release(
        self, compiled: CompiledQuery, result: QueryRows, *, catalog: CatalogView
    ) -> ReleasedResult:
        _check_authority(compiled, catalog)
        columns = _classify(compiled, catalog)
        if result.columns != tuple(c.name for c in columns):
            raise _withheld(ToolErrorCode.INTERNAL_ERROR, "shape_mismatch")
        rows: list[tuple[Cell, ...]] = []
        masked = 0
        masked_columns: set[str] = set()
        size = 0
        truncation = None if result.complete else TruncationReason.SOURCE
        for raw in result.rows:
            if len(rows) >= self._limits.max_rows:
                truncation = TruncationReason.ROWS
                break
            row, row_masked = _sanitize_row(raw, columns)
            size += _row_size(row)
            if size > self._limits.max_bytes:
                truncation = TruncationReason.BYTES
                break
            rows.append(row)
            masked += len(row_masked)
            masked_columns.update(row_masked)
        return ReleasedResult(
            columns=columns,
            rows=tuple(rows),
            received_rows=len(result.rows),
            truncation=truncation,
            masked_cells=masked,
            masked_columns=tuple(sorted(masked_columns)),
            catalog_version=compiled.catalog_version,
            entitlement_version=compiled.entitlement_version,
        )


def _check_authority(compiled: CompiledQuery, catalog: CatalogView) -> None:
    if not catalog.relations:
        raise _withheld(ToolErrorCode.ACCESS_DENIED, "no_product_scope")
    if catalog.entitlement_version != compiled.entitlement_version:
        raise _withheld(ToolErrorCode.ACCESS_DENIED, "stale_authorization")
    if catalog.catalog_version != compiled.catalog_version:
        raise _withheld(ToolErrorCode.FIELD_UNAVAILABLE, "catalog_changed")
    for relation in compiled.relations:
        if catalog.relation(relation) is None:
            raise _withheld(ToolErrorCode.FIELD_UNAVAILABLE, "catalog_changed")
    for ref in compiled.fields:
        _published(catalog, ref)


def _published(catalog: CatalogView, ref: FieldRef) -> FieldView:
    relation = catalog.relation(ref.relation)
    field = relation.field(ref.field) if relation else None
    if field is None:
        raise _withheld(ToolErrorCode.FIELD_UNAVAILABLE, "catalog_changed")
    if not _sources_allowed(field):
        raise _withheld(ToolErrorCode.INTERNAL_ERROR, "forbidden_source")
    return field


def _sources_allowed(field: FieldView) -> bool:
    """Re-assert the catalog's source rules (defense in depth)."""
    for source in field.sources:
        column = source.column
        if column in DIRECT_IDENTIFIER_COLUMNS:
            return False
        if column in EXACT_AGE_COLUMNS and field.derivation is not Derivation.AGE_BAND:
            return False
        if (
            column in IDENTIFIER_COLUMNS
            and source.table != PRODUCT_KEY_TABLE
            and field.derivation
            not in (Derivation.OPAQUE_REFERENCE, Derivation.PERMITTED_ITEM_COUNT)
        ):
            return False
    return True


def _classify(
    compiled: CompiledQuery, catalog: CatalogView
) -> tuple[ReleasedColumn, ...]:
    columns: list[ReleasedColumn] = []
    for output in compiled.outputs:
        fields = [(ref, _published(catalog, ref)) for ref in output.sources]
        role = ColumnRole.VALUE
        if output.direct and len(fields) == 1:
            derivation = fields[0][1].derivation
            if derivation is Derivation.OPAQUE_REFERENCE:
                role = ColumnRole.REFERENCE
            elif derivation is Derivation.AGE_BAND:
                role = ColumnRole.AGE_BAND
        sources = tuple(sorted(output.sources, key=lambda r: (r.relation, r.field)))
        columns.append(ReleasedColumn(output.name, sources, role))
    return tuple(columns)


def _sanitize_row(
    raw: Sequence[object], columns: tuple[ReleasedColumn, ...]
) -> tuple[tuple[Cell, ...], list[str]]:
    if isinstance(raw, str | bytes) or len(raw) != len(columns):
        raise _withheld(ToolErrorCode.INTERNAL_ERROR, "shape_mismatch")
    cells: list[Cell] = []
    masked: list[str] = []
    for value, column in zip(raw, columns, strict=True):
        cell = _scalar(value)
        if column.role is ColumnRole.REFERENCE:
            if cell is not None and not is_reference(cell, column.sources[0].field):
                raise _withheld(ToolErrorCode.INTERNAL_ERROR, "invalid_reference")
        elif column.role is ColumnRole.AGE_BAND:
            if cell is not None and not is_age_band(cell):
                raise _withheld(ToolErrorCode.INTERNAL_ERROR, "invalid_age_band")
        elif isinstance(cell, str) and screen_text(cell) & _MASKED_FINDINGS:
            cell = MASK
            masked.append(column.name)
        cells.append(cell)
    return tuple(cells), masked


def _scalar(value: object) -> Cell:
    if value is None:
        return None
    if isinstance(value, float) and value != value:  # NaN stays "no value"
        return None
    if isinstance(value, _SCALARS):
        return value
    raise _withheld(ToolErrorCode.INTERNAL_ERROR, "unsupported_value")


def _row_size(row: tuple[Cell, ...]) -> int:
    # Approximate serialized size: value text plus separators/quotes.
    return sum(len(str(cell).encode()) + 4 for cell in row) + 2


def _withheld(code: ToolErrorCode, reason: str) -> ResultWithheld:
    messages = {
        "no_product_scope": "No product data is available to you",
        "stale_authorization": "Your access changed; the query must be run again",
        "catalog_changed": "A field used by this query is no longer available",
    }
    return ResultWithheld(
        code, reason, messages.get(reason, "The result could not be released safely")
    )
