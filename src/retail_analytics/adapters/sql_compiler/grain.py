"""Aggregate-only customer demographics: a grain check over resolved scopes.

Demographic fields (catalog ``FieldView.demographic``: country, state,
age_band) may reach a result only as group-level statistics. This module
decides that from verified lineage, never from names or text, and fails
closed. For every scope it derives:

- ``demographic``: the scope reads a demographic field anywhere (projection,
  filter, grouping, ordering), directly or through a CTE, derived table or
  subquery. Influence through a filter counts: listing the items of customers
  in one state discloses their state.
- ``identity`` values: an expression carries an identity when it passes an
  opaque customer/order/item reference through (also via MIN/MAX, CASE,
  string functions or derived aliases). Counting references (``COUNT``,
  ``COUNT(DISTINCT ...)``, ``COUNTIF``) and comparing them yield numbers and
  booleans, not identities.
- ``individual``: rows are one customer, order or item: a non-aggregated
  read of a relation, a GROUP BY/DISTINCT whose key carries an identity, or
  a derived source that is individual itself.
- ``targeted``: the rows were selected by identity: a comparison involving
  an identity (``customer_ref = @r``, ``IN ('cus_..')``, range or LIKE on a
  reference, ``COUNTIF(order_ref = ...)``), or LIMIT over individual rows
  (top-N customers). Semi-joins ``ref IN (SELECT ref ...)`` are not
  comparisons with chosen identities; the subquery is judged itself.
  Declared join conditions are relation links, not targeting.

A query whose output is demographic-influenced is accepted only when its
final rows are group-level (not individual), no row set feeding it was
identity-targeted, and no output column carries an identity. Groups are not
size-checked: a naturally small group (even one customer) is a group-level
statistic; selecting people by reference or rank is not.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass

from sqlglot import exp
from sqlglot.optimizer.scope import Scope

from retail_analytics.adapters.sql_compiler.errors import unsupported
from retail_analytics.application.contracts.query_compiler import DemographicUse
from retail_analytics.domain.catalog import CatalogView, FieldView

DEMOGRAPHIC_FEEDBACK = (
    "Customer demographics (country, state, age_band) are available only as "
    "group-level statistics: GROUP BY the demographic and aggregate measures "
    "(COUNT(DISTINCT customer_ref), SUM, AVG). Do not show them with customer, "
    "order or item references, for specific references, for rank-selected "
    "customers (LIMIT over customers) or per row/customer. A profile of one "
    "customer cannot be produced."
)

# Nodes whose value is a boolean: comparing a reference yields no identity.
_BOOLEAN: tuple[type[exp.Expr], ...] = (
    exp.EQ,
    exp.NEQ,
    exp.GT,
    exp.GTE,
    exp.LT,
    exp.LTE,
    exp.Is,
    exp.Like,
    exp.Between,
    exp.In,
    exp.And,
    exp.Or,
    exp.Not,
    exp.Exists,
)
# Comparisons that select rows by the compared values.
_COMPARISONS: tuple[type[exp.Expr], ...] = (
    exp.EQ,
    exp.NEQ,
    exp.GT,
    exp.GTE,
    exp.LT,
    exp.LTE,
    exp.Is,
    exp.Like,
    exp.Between,
    exp.In,
)
# Aggregates that turn identities into numbers.
_COUNTS: tuple[type[exp.Expr], ...] = (exp.Count, exp.CountIf)


class GrainUnverifiable(Exception):
    """The structure could not be analyzed (callers fail closed)."""


@dataclass(frozen=True, slots=True)
class ScopeFacts:
    demographic: bool
    individual: bool
    targeted: bool
    identity_outputs: frozenset[str]


def demographic_use(
    root: exp.Select, scopes: list[Scope], catalog: CatalogView
) -> DemographicUse:
    """Classify a resolved query or raise ``QueryRejected`` (correctable)."""
    facts = analyze(root, scopes, catalog)
    if not facts.demographic:
        return DemographicUse.NONE
    if facts.individual or facts.targeted or facts.identity_outputs:
        raise unsupported("individual_demographics", DEMOGRAPHIC_FEEDBACK)
    return DemographicUse.AGGREGATE


def analyze(root: exp.Select, scopes: list[Scope], catalog: CatalogView) -> ScopeFacts:
    by_select = {id(s.expression): s for s in scopes}
    if id(root) not in by_select:
        raise GrainUnverifiable("root scope missing")
    return _Analyzer(by_select, catalog).facts(by_select[id(root)])


class _Analyzer:
    def __init__(self, by_select: Mapping[int, Scope], catalog: CatalogView) -> None:
        self._by_select = by_select
        self._catalog = catalog
        self._memo: dict[int, ScopeFacts] = {}
        self._active: set[int] = set()

    def facts(self, scope: Scope) -> ScopeFacts:
        key = id(scope.expression)
        found = self._memo.get(key)
        if found is not None:
            return found
        if key in self._active:
            raise GrainUnverifiable("recursive scope")
        self._active.add(key)
        try:
            result = self._compute(scope)
        finally:
            self._active.discard(key)
        self._memo[key] = result
        return result

    # --- per scope -----------------------------------------------------------

    def _compute(self, scope: Scope) -> ScopeFacts:
        select = scope.expression
        if not isinstance(select, exp.Select):
            raise GrainUnverifiable("not a select")
        sources = [source for _, source in scope.selected_sources.values()]
        derived = [self.facts(s) for s in sources if isinstance(s, Scope)]
        leaves = [s for s in sources if isinstance(s, exp.Table)]
        if len(derived) + len(leaves) != len(sources):
            raise GrainUnverifiable("unknown source")
        nested = [(self.facts(child), _value_use(child)) for child in _nested(scope)]

        demographic = any(f.demographic for f in derived) or any(
            f.demographic for f, _ in nested
        )
        for column in scope.columns:
            field = self._leaf_field(scope, column)
            if field is not None and field.demographic:
                demographic = True

        items = list(select.expressions)
        group = select.args.get("group")
        distinct = select.args.get("distinct") is not None
        aggregated = (
            group is not None
            or distinct
            or any(
                isinstance(node, exp.AggFunc)
                for item in [*items, select.args.get("having")]
                if item is not None
                for node in _own_nodes(item)
            )
        )
        if group is not None:
            individual = any(
                self._identity(scope, _group_key(key, items))
                for key in group.expressions
            )
        elif distinct:
            individual = any(self._identity(scope, item) for item in items)
        elif aggregated:
            individual = False
        elif leaves:
            individual = True
        elif derived:
            individual = any(f.individual for f in derived)
        else:
            individual = False
        individual = individual or any(f.individual for f, value in nested if value)

        targeted = any(f.targeted for f in derived) or any(
            f.targeted for f, _ in nested
        )
        if select.args.get("limit") is not None and (
            individual or (not aggregated and any(f.individual for f in derived))
        ):
            targeted = True
        if not targeted:
            targeted = self._compares_identity(scope, select)

        identity_outputs = frozenset(
            item.alias_or_name for item in items if self._identity(scope, item)
        )
        return ScopeFacts(demographic, individual, targeted, identity_outputs)

    def _leaf_field(self, scope: Scope, column: exp.Column) -> FieldView | None:
        if not column.table:
            return None
        entry = scope.selected_sources.get(column.table)
        if entry is None:
            raise GrainUnverifiable("unresolved column")
        source = entry[1]
        if not isinstance(source, exp.Table):
            return None
        relation = self._catalog.relation(source.name)
        field = relation.field(column.name) if relation else None
        if field is None:
            raise GrainUnverifiable("unpublished column")
        return field

    def _column_identity(self, scope: Scope, column: exp.Column) -> bool:
        if not column.table:
            # An unqualified ORDER BY output alias: judged through its item.
            select = scope.expression
            for item in select.expressions:
                if item.alias_or_name == column.name and item is not column:
                    return self._identity(scope, item)
            raise GrainUnverifiable("unresolved alias")
        entry = scope.selected_sources.get(column.table)
        if entry is None:
            raise GrainUnverifiable("unresolved column")
        source = entry[1]
        if isinstance(source, Scope):
            return column.name in self.facts(source).identity_outputs
        field = self._leaf_field(scope, column)
        return field is not None and field.is_identity

    def _identity(self, scope: Scope, node: exp.Expr) -> bool:
        """Whether ``node``'s value passes an identity through."""
        if isinstance(node, (*_COUNTS, *_BOOLEAN)):
            return False
        if isinstance(node, exp.Column):
            return self._column_identity(scope, node)
        if isinstance(node, exp.Select):
            child = self._by_select.get(id(node))
            if child is None:
                raise GrainUnverifiable("unknown subquery")
            return bool(self.facts(child).identity_outputs)
        return any(self._identity(scope, child) for child in node.iter_expressions())

    def _compares_identity(self, scope: Scope, select: exp.Select) -> bool:
        for part in _filtered_parts(select):
            for node in _own_nodes(part):
                if not isinstance(node, _COMPARISONS):
                    continue
                operands: list[exp.Expr] = []
                for key, value in node.args.items():
                    if key == "query" or value is None:
                        continue
                    operands.extend(
                        v for v in _as_list(value) if isinstance(v, exp.Expr)
                    )
                if isinstance(node, exp.In) and node.args.get("query") is not None:
                    # Semi-join: membership in a subquery's row set.
                    continue
                if any(self._identity(scope, operand) for operand in operands):
                    return True
        return False


def _as_list(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else [value]


def _group_key(key: exp.Expr, items: list[exp.Expr]) -> exp.Expr:
    """A positional GROUP BY key is the SELECT item it names."""
    if isinstance(key, exp.Literal) and key.is_int:
        position = int(key.this)
        if not 1 <= position <= len(items):
            raise GrainUnverifiable("group position out of range")
        return items[position - 1]
    return key


def _filtered_parts(select: exp.Select) -> Iterator[exp.Expr]:
    """Every part of a SELECT except joins (declared links) and nested queries."""
    for key, value in select.args.items():
        if key in ("joins", "with_", "from_") or value is None:
            continue
        for item in _as_list(value):
            if isinstance(item, exp.Expr):
                yield item


def _own_nodes(node: exp.Expr) -> Iterator[exp.Expr]:
    """Walk ``node`` without entering nested SELECTs."""
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, exp.Select):
            continue
        yield current
        stack.extend(current.iter_expressions())


def _nested(scope: Scope) -> list[Scope]:
    return list(scope.subquery_scopes)


def _value_use(child: Scope) -> bool:
    """Whether a subquery's rows are used as values (not IN/EXISTS membership)."""
    node: exp.Expr | None = child.expression
    parent = node.parent if node is not None else None
    if isinstance(parent, exp.Subquery):
        node, parent = parent, parent.parent
    if isinstance(parent, exp.Exists):
        return False
    return not (isinstance(parent, exp.In) and node is parent.args.get("query"))
