"""Trusted source bindings: how each logical relation reads physical data.

A binding is executor-owned SQL. It applies the product scope at every
physical source *before* the model's query sees a row, and projects only
reviewed logical fields, each built from its catalog derivation. The model's
query can only ever read these projections.

Derivations whose design belongs to the privacy boundary (opaque references
and age bands) are delegated to a :class:`TrustedDerivations` implementation.
Without one they are unavailable: a query that needs them fails closed.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol

import sqlglot
from sqlglot import exp

from retail_analytics.application.query_compiler import ParameterType, QueryParameter
from retail_analytics.domain.catalog import (
    DIRECT_IDENTIFIER_COLUMNS,
    EXACT_AGE_COLUMNS,
    IDENTIFIER_COLUMNS,
    PRODUCT_KEY_TABLE,
    Derivation,
    FieldView,
    RelationView,
)

SCOPE_PARAMETER = "_policy_product_ids"
TRUSTED_SOURCE = "trusted_source"
# Projected when a query references a relation but none of its fields
# (``COUNT(*)``); the reserved name cannot be referenced by model SQL.
_ROW_MARKER = "_policy_row"
_DATASET = re.compile(r"[a-z][a-z0-9-]{2,62}\.[A-Za-z0-9_]{1,1024}")


class DerivationUnavailable(Exception):
    """No trusted implementation exists for a field's derivation."""


class BindingError(Exception):
    """A catalog field cannot be bound safely (review or configuration error)."""


class TrustedDerivations(Protocol):
    """Builds expressions for privacy-owned derivations.

    Implementations receive raw source expressions that exist only inside the
    trusted binding and must return an expression that does not reveal them.
    ``kind`` is the logical reference name (``customer_ref``...): the same kind
    must map the same raw key to the same reference in every relation so that
    declared joins match. Any parameters (for example a key) must be named with
    the ``_policy_`` prefix; they are added as trusted parameters.
    """

    def opaque_reference(self, kind: str, raw_key: exp.Expr) -> exp.Expr:
        """Raise DerivationUnavailable when references are not configured."""
        ...

    def age_band(self, raw_age: exp.Expr) -> exp.Expr:
        """Raise DerivationUnavailable when age bands are not configured."""
        ...

    def parameters(self) -> tuple[QueryParameter, ...]: ...


class UnavailableDerivations:
    """Default until the privacy boundary provides real implementations."""

    def opaque_reference(self, kind: str, raw_key: exp.Expr) -> exp.Expr:
        raise DerivationUnavailable(kind)

    def age_band(self, raw_age: exp.Expr) -> exp.Expr:
        raise DerivationUnavailable("age_band")

    def parameters(self) -> tuple[QueryParameter, ...]:
        return ()


@dataclass(frozen=True, slots=True)
class _Template:
    """Trusted FROM/WHERE/GROUP BY of one relation, with table aliases."""

    aliases: Mapping[str, str]
    body: str
    group_by: tuple[tuple[str, str], ...] = ()


# Product scope is applied to the item rows every relation is reached through.
# ``{ds}`` is the configured dataset; ``{scope}`` the trusted scope parameter.
_TEMPLATES: Mapping[str, _Template] = {
    "sales_items": _Template(
        {"order_items": "i", "orders": "o"},
        "FROM `{ds}.order_items` AS i JOIN `{ds}.orders` AS o "
        "ON o.order_id = i.order_id "
        "WHERE i.product_id IN UNNEST(@{scope})",
    ),
    "products": _Template(
        {"products": "p"},
        "FROM `{ds}.products` AS p WHERE p.id IN UNNEST(@{scope})",
    ),
    # One row per order, counting only permitted items: full-basket fields of
    # the source order are never projected.
    "orders": _Template(
        {"orders": "o", "order_items": "i"},
        "FROM `{ds}.orders` AS o JOIN `{ds}.order_items` AS i "
        "ON i.order_id = o.order_id "
        "WHERE i.product_id IN UNNEST(@{scope})",
        group_by=(
            ("orders", "order_id"),
            ("orders", "user_id"),
            ("orders", "created_at"),
        ),
    ),
    # Customers reached through permitted items only.
    "customers": _Template(
        {"users": "u"},
        "FROM `{ds}.users` AS u WHERE u.id IN ("
        "SELECT i.user_id FROM `{ds}.order_items` AS i "
        "WHERE i.product_id IN UNNEST(@{scope}))",
    ),
}


class SourceBindings:
    """Builds product-scoped projections of logical relations."""

    def __init__(self, dataset: str, derivations: TrustedDerivations) -> None:
        if not _DATASET.fullmatch(dataset):
            raise ValueError("dataset must be '<project>.<dataset>'")
        self._derivations = derivations
        self._bodies = {
            relation: _parse_body(template, dataset)
            for relation, template in _TEMPLATES.items()
        }
        self.physical_tables: frozenset[tuple[str, str, str]] = frozenset(
            (table.catalog, table.db, table.name)
            for body in self._bodies.values()
            for table in body.find_all(exp.Table)
        )
        for parameter in derivations.parameters():
            if not parameter.name.startswith("_policy_") or not parameter.trusted:
                raise ValueError("derivation parameters must be trusted _policy_ names")

    def supports(self, relation: str) -> bool:
        return relation in _TEMPLATES

    def trusted_parameters(
        self, product_ids: Iterable[int]
    ) -> tuple[QueryParameter, ...]:
        scope = QueryParameter(
            SCOPE_PARAMETER,
            ParameterType.INT64,
            tuple(sorted(product_ids)),
            array=True,
            trusted=True,
        )
        return (scope, *self._derivations.parameters())

    def projection(self, relation: RelationView, fields: Iterable[str]) -> exp.Select:
        """A scoped SELECT exposing exactly ``fields`` of ``relation``.

        Raises BindingError or DerivationUnavailable; never falls back.
        """
        template = _TEMPLATES[relation.name]
        select = self._bodies[relation.name].copy()
        projections: list[exp.Expr] = []
        for name in sorted(set(fields)):
            field = relation.field(name)
            if field is None:
                raise BindingError(f"{relation.name}.{name} is not in the view")
            projections.append(
                exp.alias_(self._field_expression(field, template), name, quoted=True)
            )
        if not projections:
            projections.append(exp.alias_(exp.true(), _ROW_MARKER, quoted=True))
        select.set("expressions", projections)
        if template.group_by:
            select.set(
                "group",
                exp.Group(
                    expressions=[
                        _column(template, table, column)
                        for table, column in template.group_by
                    ]
                ),
            )
        for table in select.find_all(exp.Table):
            table.meta[TRUSTED_SOURCE] = True
        return select

    def _field_expression(self, field: FieldView, template: _Template) -> exp.Expr:
        _check_sources(field)
        columns = [_column(template, s.table, s.column) for s in field.sources]
        derivation = field.derivation
        if derivation is Derivation.PERMITTED_ITEM_COUNT:
            if not template.group_by:
                raise BindingError(f"{field.name}: item count needs a grouped relation")
            return exp.Count(this=exp.Distinct(expressions=[columns[0]]))
        if len(columns) != 1:
            raise BindingError(f"{field.name}: expected exactly one source column")
        (column,) = columns
        if template.group_by and not _grouped(template, field):
            raise BindingError(f"{field.name}: source is not a grouping key")
        if derivation is Derivation.DIRECT:
            return column
        if derivation is Derivation.DATE_OF_TIMESTAMP:
            # BigQuery converts TIMESTAMP to its UTC calendar date.
            return exp.cast(column, exp.DataType.Type.DATE)
        if derivation is Derivation.OPAQUE_REFERENCE:
            return self._derivations.opaque_reference(field.name, column)
        if derivation is Derivation.AGE_BAND:
            return self._derivations.age_band(column)
        raise BindingError(f"{field.name}: unknown derivation")


def _parse_body(template: _Template, dataset: str) -> exp.Select:
    sql = "SELECT 1 " + template.body.format(ds=dataset, scope=SCOPE_PARAMETER)
    parsed = sqlglot.parse_one(sql, read="bigquery")
    if not isinstance(parsed, exp.Select):
        raise BindingError("binding template must be a SELECT")
    return parsed


def _column(template: _Template, table: str, column: str) -> exp.Column:
    alias = template.aliases.get(table)
    if alias is None:
        raise BindingError(f"source table {table} is not part of the binding")
    return exp.column(column, table=alias)


def _grouped(template: _Template, field: FieldView) -> bool:
    keys = set(template.group_by)
    return all((s.table, s.column) in keys for s in field.sources)


def _check_sources(field: FieldView) -> None:
    """Defense in depth: re-assert the catalog's source-column rules."""
    for source in field.sources:
        column = source.column
        if column in DIRECT_IDENTIFIER_COLUMNS:
            raise BindingError(f"{field.name}: direct identifier source")
        if column in EXACT_AGE_COLUMNS and field.derivation is not Derivation.AGE_BAND:
            raise BindingError(f"{field.name}: exact age without an age band")
        if (
            column in IDENTIFIER_COLUMNS
            and source.table != PRODUCT_KEY_TABLE
            and field.derivation
            not in (Derivation.OPAQUE_REFERENCE, Derivation.PERMITTED_ITEM_COUNT)
        ):
            raise BindingError(f"{field.name}: raw key without an opaque reference")
