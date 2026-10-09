"""SQLGlot implementation of the restricted analytical SQL compiler.

Sequence (each step fails closed with a :class:`QueryRejected`):

1. Check the trusted authority: non-empty product scope, matching versions.
2. Validate analysis values (scalar, typed, non-reserved names).
3. Parse exactly one BigQuery SELECT, within size limits.
4. Apply the grammar allowlist (``grammar``) and strip comments.
5. Resolve sources with lexical scope: CTE/subquery names shadow logical
   relations, every leaf must be a relation of the executive's catalog view,
   unused CTEs and referenced-parameter mismatches are rejected.
6. Qualify every column (and re-apply the grammar), then require every
   column to resolve to a published
   field or a derived output; only an ORDER BY output alias may stay
   unqualified (SQLGlot alone accepts unresolved HAVING references).
7. Reject correlation, duplicate output names and any join not declared in
   the catalog (many-to-one equality from a joined leaf to a new leaf).
8. Record output lineage and the query's exact date window (if any), then
   replace literals with typed parameters.
9. Replace each leaf with a trusted, product-scoped projection of exactly the
   referenced fields (``bindings``).
10. Postconditions: every remaining table is a trusted physical source, every
    parameter is accounted for, and the emitted SQL reparses to one SELECT.

Parsing and qualification are components of the boundary, not the boundary.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import TypeGuard

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel, OptimizeError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, traverse_scope

from retail_analytics.adapters.sql_compiler.bindings import (
    TRUSTED_SOURCE,
    BindingError,
    DerivationUnavailable,
    SourceBindings,
    TrustedDerivations,
    UnavailableDerivations,
)
from retail_analytics.adapters.sql_compiler.errors import (
    field_unavailable,
    reject,
    unsupported,
)
from retail_analytics.adapters.sql_compiler.grain import (
    GrainUnverifiable,
    analyze,
    demographic_use,
)
from retail_analytics.adapters.sql_compiler.grammar import check_grammar, is_reserved
from retail_analytics.application.contracts.query_compiler import (
    AnalysisQuery,
    CompiledQuery,
    DemographicUse,
    FieldRef,
    OutputColumn,
    ParameterType,
    QueryParameter,
    ScalarValue,
)
from retail_analytics.application.contracts.sql_dialect import DERIVED_JOIN_FEEDBACK
from retail_analytics.application.query_compiler import (
    DEFAULT_MAXIMUM_BYTES_BILLED,
    QueryRejected,
)
from retail_analytics.domain.access import ProductScope, is_valid_product_id
from retail_analytics.domain.catalog import (
    CatalogHealth,
    CatalogView,
    FieldType,
    LogicalCatalog,
    build_view,
)
from retail_analytics.domain.logical_catalog import default_logical_catalog
from retail_analytics.domain.operations import ToolErrorCode
from retail_analytics.domain.periods import DateWindow

_DIALECT = "bigquery"
_SCHEMA_TYPES: Mapping[FieldType, str] = {
    FieldType.STRING: "STRING",
    FieldType.INTEGER: "INT64",
    FieldType.NUMBER: "FLOAT64",
    FieldType.DATE: "DATE",
    FieldType.REFERENCE: "STRING",
}
_UNKNOWN_COLUMN = re.compile(r"(?:Unknown column: |Column ')(\w{1,64})")


@dataclass(frozen=True, slots=True)
class CompilerLimits:
    max_sql_chars: int = 20_000
    max_nodes: int = 2_000
    max_scopes: int = 20
    max_parameters: int = 100
    max_string_chars: int = 1_000
    maximum_bytes_billed: int = DEFAULT_MAXIMUM_BYTES_BILLED


class SqlglotQueryCompiler:
    """Implements ``QueryCompiler`` for BigQuery over a configured dataset."""

    def __init__(
        self,
        dataset: str,
        *,
        derivations: TrustedDerivations | None = None,
        limits: CompilerLimits | None = None,
    ) -> None:
        self._bindings = SourceBindings(
            dataset, derivations or UnavailableDerivations()
        )
        self._limits = limits or CompilerLimits()

    def compile(
        self, query: AnalysisQuery, *, catalog: CatalogView, scope: ProductScope
    ) -> CompiledQuery:
        product_ids = _authorized_products(catalog, scope)
        values = _analysis_values(query.parameters, self._limits)
        tree = self._parse(query.sql)
        try:
            return self._compile(tree, values, catalog, product_ids)
        except QueryRejected:
            raise
        except Exception as error:
            # Fail closed: an unexpected parser/optimizer exception on model SQL is a
            # rejection, never a crash and never an accepted query.
            raise reject(
                ToolErrorCode.INVALID_QUERY,
                "unresolvable_query",
                "The query cannot be resolved within the supported SQL subset",
                cause_type=type(error).__name__,
            ) from None

    def _parse(self, sql: str) -> exp.Select:
        if len(sql) > self._limits.max_sql_chars:
            raise reject(
                ToolErrorCode.INVALID_QUERY, "query_too_large", "The query is too long"
            )
        try:
            statements = sqlglot.parse(sql, read=_DIALECT, error_level=ErrorLevel.RAISE)
        except Exception as error:
            raise reject(
                ToolErrorCode.INVALID_QUERY,
                "syntax_error",
                "The query could not be parsed as BigQuery SQL",
                cause_type=type(error).__name__,
            ) from None
        if len(statements) != 1 or type(statements[0]) is not exp.Select:
            raise unsupported(
                "single_select_required", "Exactly one SELECT statement is supported"
            )
        tree = statements[0]
        if sum(1 for _ in tree.walk()) > self._limits.max_nodes:
            raise reject(
                ToolErrorCode.INVALID_QUERY,
                "query_too_large",
                "The query is too complex",
            )
        return tree

    def _compile(
        self,
        tree: exp.Select,
        values: dict[str, QueryParameter],
        catalog: CatalogView,
        product_ids: tuple[int, ...],
    ) -> CompiledQuery:
        check_grammar(tree)
        _check_sources(tree, catalog, self._bindings, self._limits)
        _check_parameter_references(tree, values)
        tree = _qualify(tree, catalog)
        _resolve_group_aliases(tree)
        # Qualification can introduce nodes (a bare table alias becomes a
        # whole-row TableColumn); the result must still be inside the subset.
        check_grammar(tree)
        scopes = list(traverse_scope(tree))
        _check_columns(tree, scopes, catalog)
        for scope in scopes:
            _check_scope(scope, catalog)
        demographics = _demographic_use(tree, scopes, catalog)
        outputs = _lineage(tree, scopes)
        window = _date_window(scopes, catalog, values)
        literal_parameters = _parameterize(tree, len(values))
        logical_sql = tree.sql(dialect=_DIALECT)
        relations, fields = self._bind(scopes, catalog)
        referenced = {p.name for p in tree.find_all(exp.Parameter)}
        trusted = self._bindings.trusted_parameters(product_ids)
        parameters = (
            *values.values(),
            *literal_parameters,
            *(p for p in trusted if p.name in referenced),
        )
        sql = self._emit(tree, parameters)
        return CompiledQuery(
            sql=sql,
            parameters=parameters,
            logical_sql=logical_sql,
            relations=frozenset(relations),
            fields=frozenset(fields),
            outputs=outputs,
            catalog_version=catalog.catalog_version,
            entitlement_version=catalog.entitlement_version,
            maximum_bytes_billed=self._limits.maximum_bytes_billed,
            date_window=window,
            demographic_use=demographics,
        )

    def _bind(
        self, scopes: list[Scope], catalog: CatalogView
    ) -> tuple[set[str], set[FieldRef]]:
        relations: set[str] = set()
        fields: set[FieldRef] = set()
        for scope in scopes:
            for alias, (_, source) in scope.selected_sources.items():
                if not isinstance(source, exp.Table):
                    continue
                view = catalog.relation(source.name)
                if view is None:  # resolved earlier; defensive
                    raise field_unavailable(source.name, None)
                used = {c.name for c in scope.columns if c.table == alias}
                try:
                    body = self._bindings.projection(view, used)
                except DerivationUnavailable:
                    raise reject(
                        ToolErrorCode.FIELD_UNAVAILABLE,
                        "derivation_unavailable",
                        "A referenced field is not available yet",
                        relation=view.name,
                    ) from None
                except BindingError:
                    raise reject(
                        ToolErrorCode.FIELD_UNAVAILABLE,
                        "binding_unavailable",
                        "A referenced field cannot be served safely",
                        relation=view.name,
                    ) from None
                source.replace(
                    exp.Subquery(
                        this=body,
                        alias=exp.TableAlias(
                            this=exp.to_identifier(alias, quoted=True)
                        ),
                    )
                )
                relations.add(view.name)
                fields.update(FieldRef(view.name, name) for name in used)
        return relations, fields

    def _emit(self, tree: exp.Select, parameters: tuple[QueryParameter, ...]) -> str:
        allowed = self._bindings.physical_tables
        for table in _physical_tables(tree):
            key = (table.catalog, table.db, table.name)
            if not table.meta.get(TRUSTED_SOURCE) or key not in allowed:
                raise _internal("unbound source after binding")
        names = {p.name for p in parameters}
        if len(names) != len(parameters):
            raise _internal("duplicate parameter names")
        if any(p.name.startswith("_policy_") != p.trusted for p in parameters):
            raise _internal("trusted parameter naming violated")
        if any(p.name not in names for p in tree.find_all(exp.Parameter)):
            raise _internal("unaccounted parameter")
        sql = tree.sql(dialect=_DIALECT, unsupported_level=ErrorLevel.RAISE)
        reparsed = sqlglot.parse(sql, read=_DIALECT)
        if len(reparsed) != 1 or type(reparsed[0]) is not exp.Select:
            raise _internal("emitted SQL is not one SELECT")
        for table in _physical_tables(reparsed[0]):
            if (table.catalog, table.db, table.name) not in allowed:
                raise _internal("emitted SQL references an unknown source")
        return sql


def _demographic_use(
    tree: exp.Select, scopes: list[Scope], catalog: CatalogView
) -> DemographicUse:
    try:
        return demographic_use(tree, scopes, catalog)
    except GrainUnverifiable:
        # Fail closed: a structure the grain check cannot follow is refused
        # whether or not it reads demographics.
        raise reject(
            ToolErrorCode.UNSUPPORTED_SQL,
            "grain_unverifiable",
            "The query's result grain could not be verified; simplify it",
        ) from None


def _physical_tables(tree: exp.Select) -> list[exp.Table]:
    """Table nodes that are not references to a CTE, resolved by scope."""
    cte_references = {
        id(node)
        for scope in traverse_scope(tree)
        for node, source in scope.selected_sources.values()
        if isinstance(source, Scope)
    }
    return [t for t in tree.find_all(exp.Table) if id(t) not in cte_references]


def _internal(detail: str) -> QueryRejected:
    # Postcondition failures are compiler defects; the detail stays internal.
    del detail
    return reject(
        ToolErrorCode.INTERNAL_ERROR,
        "binding_postcondition",
        "The query could not be prepared safely",
    )


# --- authority and values ---------------------------------------------------------


def _authorized_products(catalog: CatalogView, scope: ProductScope) -> tuple[int, ...]:
    if scope.is_empty or not catalog.relations:
        raise reject(
            ToolErrorCode.ACCESS_DENIED,
            "no_product_scope",
            "No product data is available to you",
        )
    if catalog.entitlement_version != scope.entitlement_version:
        raise reject(
            ToolErrorCode.ACCESS_DENIED,
            "stale_authorization",
            "Your access changed; the request must be re-authorized",
        )
    if not all(is_valid_product_id(p) for p in scope.product_ids):
        raise reject(
            ToolErrorCode.ACCESS_DENIED,
            "invalid_product_scope",
            "No product data is available to you",
        )
    return tuple(sorted(int(p) for p in scope.product_ids))


def _analysis_values(
    supplied: Mapping[str, ScalarValue], limits: CompilerLimits
) -> dict[str, QueryParameter]:
    if len(supplied) > limits.max_parameters:
        raise reject(
            ToolErrorCode.INVALID_INPUT, "too_many_parameters", "Too many parameters"
        )
    values: dict[str, QueryParameter] = {}
    seen: set[str] = set()
    for name, value in supplied.items():
        if not (
            isinstance(name, str)
            and name.isascii()
            and name.isidentifier()
            and len(name) <= 64
            and not is_reserved(name)
            and name.casefold() not in seen
        ):
            raise reject(
                ToolErrorCode.INVALID_INPUT,
                "invalid_parameter_name",
                "Parameter names must be unique identifiers without reserved prefixes",
                field=name if isinstance(name, str) else None,
            )
        seen.add(name.casefold())
        values[name] = QueryParameter(name, _value_type(name, value, limits), value)
    return values


def _value_type(name: str, value: object, limits: CompilerLimits) -> ParameterType:
    if type(value) is bool:
        return ParameterType.BOOL
    if type(value) is int and -(2**63) <= value < 2**63:
        return ParameterType.INT64
    if type(value) is float and math.isfinite(value):
        return ParameterType.FLOAT64
    if type(value) is Decimal and value.is_finite():
        return ParameterType.NUMERIC
    if type(value) is date:
        return ParameterType.DATE
    if type(value) is str and len(value) <= limits.max_string_chars:
        return ParameterType.STRING
    raise reject(
        ToolErrorCode.INVALID_INPUT,
        "invalid_parameter_value",
        "Parameters must be scalar strings, numbers, booleans or dates",
        field=name,
    )


def _check_parameter_references(
    tree: exp.Select, values: Mapping[str, QueryParameter]
) -> None:
    referenced = {p.name for p in tree.find_all(exp.Parameter)}
    if referenced != set(values):
        raise reject(
            ToolErrorCode.INVALID_INPUT,
            "parameter_mismatch",
            "Supply exactly the parameters the query references",
        )


# --- sources and resolution ---------------------------------------------------------


def _check_sources(
    tree: exp.Select,
    catalog: CatalogView,
    bindings: SourceBindings,
    limits: CompilerLimits,
) -> None:
    scopes = list(traverse_scope(tree))
    if len(scopes) > limits.max_scopes:
        raise reject(
            ToolErrorCode.INVALID_QUERY, "query_too_large", "Too many nested queries"
        )
    used: set[int] = set()
    for scope in scopes:
        for _, source in scope.selected_sources.values():
            if isinstance(source, Scope):
                used.add(id(source.expression))
                continue
            if not isinstance(source, exp.Table):
                raise unsupported("unsupported_source", "Unsupported source")
            if catalog.relation(source.name) is None or not bindings.supports(
                source.name
            ):
                raise reject(
                    ToolErrorCode.FIELD_UNAVAILABLE,
                    "relation_unavailable",
                    "The relation is not available; use list_relations",
                    relation=source.name,
                )
    for scope in scopes:
        if scope.is_cte and id(scope.expression) not in used:
            raise unsupported("unused_cte", "Remove CTEs that the query does not use")
    # Every table node must be a source of some scope (no orphan references).
    known = {
        id(node) for scope in scopes for node, _ in scope.selected_sources.values()
    }
    if any(id(table) not in known for table in tree.find_all(exp.Table)):
        raise unsupported("unsupported_source", "Unsupported source reference")


def _qualify(tree: exp.Select, catalog: CatalogView) -> exp.Select:
    schema: dict[str, object] = {
        name: {f.name: _SCHEMA_TYPES[f.type] for f in relation.fields}
        for name, relation in catalog.relations.items()
    }
    try:
        qualified = qualify(
            tree,
            dialect=_DIALECT,
            schema=schema,
            infer_schema=False,
            expand_stars=False,
            validate_qualify_columns=True,
            allow_partial_qualification=False,
            quote_identifiers=True,
        )
    except OptimizeError as error:
        match = _UNKNOWN_COLUMN.search(str(error))
        if match:
            raise field_unavailable(None, match.group(1)) from None
        raise reject(
            ToolErrorCode.INVALID_QUERY,
            "unresolved_reference",
            "A reference is ambiguous or cannot be resolved; qualify columns "
            "with their relation alias",
        ) from None
    if not isinstance(qualified, exp.Select):
        raise _internal("qualification changed the statement type")
    return qualified


def _resolve_group_aliases(tree: exp.Select) -> None:
    """Make every GROUP BY key unambiguous: never a bare SELECT-alias name.

    A bare name may be a SELECT alias or a source column; engines disagree on
    which wins when joined relations share it. sqlglot's BigQuery generator
    also rewrites a key equal to an aliased SELECT item into that alias when
    the query has ORDER BY. Such keys become the item's position instead, which
    means the same in every engine and survives generation unchanged; a bare
    name that is not otherwise rewritten becomes the item's qualified column.
    Positions are not data literals, so nothing is parameterized.
    """
    for select in tree.find_all(exp.Select):
        group = select.args.get("group")
        if group is None:
            continue
        items = list(select.expressions)
        names = [item.alias_or_name for item in items]
        ordered = select.args.get("order") is not None
        for key in group.expressions:
            position = _selected_position(key, items, names, ordered)
            if position is None:
                continue
            inner = _inner(items[position])
            bare = isinstance(key, exp.Column) and not key.table
            if bare and not ordered and isinstance(inner, exp.Column) and inner.table:
                key.replace(inner.copy())
                continue
            key.replace(exp.Literal.number(position + 1))


def _inner(item: exp.Expr) -> exp.Expr:
    return item.this if isinstance(item, exp.Alias) else item


def _selected_position(
    key: exp.Expr, items: list[exp.Expr], names: list[str], ordered: bool
) -> int | None:
    """Index of the SELECT item a key refers to by alias or (with ORDER BY) by value."""
    if isinstance(key, exp.Column) and not key.table:
        return names.index(key.name) if names.count(key.name) == 1 else None
    if ordered:
        matches = [
            i
            for i, item in enumerate(items)
            if isinstance(item, exp.Alias) and item.this == key
        ]
        if len(matches) == 1:
            return matches[0]
    return None


def _output_names(select: exp.Expr) -> list[str]:
    # Only SELECTs have named outputs; anything else resolves to nothing.
    if not isinstance(select, exp.Select):
        return []
    return [e.alias_or_name for e in select.expressions]


def _check_columns(tree: exp.Select, scopes: list[Scope], catalog: CatalogView) -> None:
    for column in tree.find_all(exp.Column):
        if column.table:
            continue
        select = column.find_ancestor(exp.Select)
        allowed = (
            isinstance(column.parent, exp.Ordered)
            and select is not None
            and column.name in _output_names(select)
        )
        if not allowed:
            raise field_unavailable(None, column.name)
    for scope in scopes:
        for column in scope.columns:
            if not column.table:
                continue
            entry = scope.selected_sources.get(column.table)
            if entry is None:
                raise field_unavailable(None, column.name)
            source = entry[1]
            if isinstance(source, exp.Table):
                relation = catalog.relation(source.name)
                if relation is None or relation.field(column.name) is None:
                    raise field_unavailable(source.name, column.name)
            elif isinstance(source, Scope):
                if column.name not in _output_names(source.expression):
                    raise field_unavailable(None, column.name)
            else:
                raise unsupported("unsupported_source", "Unsupported source")


def _check_scope(scope: Scope, catalog: CatalogView) -> None:
    select = scope.expression
    if not isinstance(select, exp.Select):
        raise unsupported("unsupported_construct", "Only SELECT queries are supported")
    names = [n.casefold() for n in _output_names(select)]
    if len(names) != len(set(names)) or any(not n for n in names):
        raise reject(
            ToolErrorCode.INVALID_QUERY,
            "duplicate_output",
            "Output column names must be unique; add aliases",
        )
    if scope.external_columns:
        # Correlation needs column-origin proof across scopes; not supported yet.
        raise unsupported(
            "correlated_subquery", "Correlated subqueries are not supported"
        )
    _check_joins(scope, catalog)


def _leaf(scope: Scope, alias: str) -> str | None:
    entry = scope.selected_sources.get(alias)
    source = entry[1] if entry else None
    return source.name if isinstance(source, exp.Table) else None


def _check_joins(scope: Scope, catalog: CatalogView) -> None:
    select = scope.expression
    joins = select.args.get("joins") or []
    if not joins:
        return
    base = select.args["from_"].this
    base_relation = (
        _leaf(scope, base.alias_or_name) if isinstance(base, exp.Table) else None
    )
    if base_relation is None:
        raise _join_error(DERIVED_JOIN_FEEDBACK)
    present = {base.alias_or_name: base_relation}
    for join in joins:
        target = join.this
        side = (join.args.get("side") or "").upper()
        kind = (join.args.get("kind") or "").upper()
        if (
            side not in ("", "LEFT")
            or kind not in ("", "INNER", "OUTER")
            or (kind == "OUTER" and side != "LEFT")
        ):
            raise _join_error("Only INNER and LEFT joins are supported")
        relation = (
            _leaf(scope, target.alias_or_name)
            if isinstance(target, exp.Table)
            else None
        )
        if relation is None:
            raise _join_error(DERIVED_JOIN_FEEDBACK)
        if relation in present.values():
            raise _join_error("Each relation can be joined once per query level")
        _check_join_condition(
            join.args.get("on"), present, target.alias_or_name, relation, catalog
        )
        present[target.alias_or_name] = relation


def _check_join_condition(
    condition: exp.Expr | None,
    present: Mapping[str, str],
    alias: str,
    relation: str,
    catalog: CatalogView,
) -> None:
    if not (
        type(condition) is exp.EQ
        and type(condition.this) is exp.Column
        and type(condition.expression) is exp.Column
    ):
        raise _join_error("Join with a single equality on the declared join fields")
    left, right = condition.this, condition.expression
    if right.table != alias:
        left, right = right, left
    if right.table != alias or left.table not in present:
        raise _join_error(
            "The join condition must link the joined relation to an earlier one"
        )
    origin = catalog.relation(present[left.table])
    declared = origin is not None and any(
        j.field == left.name and j.target == relation and j.target_field == right.name
        for j in origin.joins
    )
    if not declared:
        raise _join_error("Use a declared join (see describe_relation)")


def _join_error(message: str) -> QueryRejected:
    return unsupported("unsupported_join", message)


# --- lineage ---------------------------------------------------------------


def _lineage(tree: exp.Select, scopes: list[Scope]) -> tuple[OutputColumn, ...]:
    by_select = {id(s.expression): s for s in scopes}
    root = by_select[id(tree)]
    return tuple(
        OutputColumn(name, *_output_lineage(root, name, by_select))
        for name in _output_names(tree)
    )


def _output_lineage(
    scope: Scope, name: str, by_select: Mapping[int, Scope]
) -> tuple[frozenset[FieldRef], bool]:
    expression = next(
        e for e in scope.expression.expressions if e.alias_or_name == name
    )
    inner = expression.this if isinstance(expression, exp.Alias) else expression
    if isinstance(inner, exp.Column):
        return _column_lineage(scope, inner, by_select)
    sources: set[FieldRef] = set()
    for node in _same_scope_nodes(inner):
        if isinstance(node, exp.Column):
            sources |= _column_lineage(scope, node, by_select)[0]
        elif isinstance(node, exp.Select):
            child = by_select[id(node)]
            for output in _output_names(node):
                sources |= _output_lineage(child, output, by_select)[0]
    return frozenset(sources), False


def _column_lineage(
    scope: Scope, column: exp.Column, by_select: Mapping[int, Scope]
) -> tuple[frozenset[FieldRef], bool]:
    entry = scope.selected_sources.get(column.table)
    source = entry[1] if entry else None
    if isinstance(source, exp.Table):
        return frozenset({FieldRef(source.name, column.name)}), True
    if isinstance(source, Scope):
        return _output_lineage(source, column.name, by_select)
    return frozenset(), False


def _same_scope_nodes(node: exp.Expr) -> Iterator[exp.Expr]:
    """Walk ``node`` but stop at nested SELECTs (yielded, not descended)."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        if isinstance(current, exp.Select):
            continue
        stack.extend(current.iter_expressions())


# --- date window ---------------------------------------------------------------------

# Logical field rows are dated by (a UTC calendar date).
DATED_FIELD = "ordered_date"
_LOWER: Mapping[type[exp.Expr], int] = {exp.GTE: 0, exp.GT: 1}
_UPPER: Mapping[type[exp.Expr], int] = {exp.LT: 0, exp.LTE: 1}
_FLIPPED: Mapping[type[exp.Expr], type[exp.Expr]] = {
    exp.GTE: exp.LTE,
    exp.GT: exp.LT,
    exp.LTE: exp.GTE,
    exp.LT: exp.GT,
    exp.EQ: exp.EQ,
}


def _date_window(
    scopes: list[Scope], catalog: CatalogView, values: Mapping[str, QueryParameter]
) -> DateWindow | None:
    """The single half-open window every dated read is filtered to.

    Conservative by design: each scope that reads a dated relation directly
    must bound its date field with constant top-level WHERE conjuncts
    (>=, >, <, <=, =, BETWEEN against DATE literals or DATE values), every
    such scope must give the same window, and the date field may not appear
    in any other filter (OR, NOT, CASE, JOIN ON or HAVING). Anything else,
    including comparisons of several periods, yields None: the period is then
    reported as not recorded rather than guessed.
    """
    windows: set[DateWindow] = set()
    for scope in scopes:
        dated = {
            alias
            for alias in scope.selected_sources
            if _is_dated(_leaf(scope, alias), catalog)
        }
        date_columns = [c for c in scope.columns if c.name == DATED_FIELD]
        if not dated:
            if any(_in_filter(c) for c in date_columns):
                return None  # filtering a derived date: not a source window
            continue
        window = _scope_window(scope, dated, date_columns, values)
        if window is None:
            return None
        windows.add(window)
    return windows.pop() if len(windows) == 1 else None


def _is_dated(relation: str | None, catalog: CatalogView) -> bool:
    view = None if relation is None else catalog.relation(relation)
    return view is not None and view.field(DATED_FIELD) is not None


def _in_filter(column: exp.Column) -> bool:
    owner = column.find_ancestor(exp.Where, exp.Join, exp.Having, exp.Select)
    return not isinstance(owner, exp.Select)


def _scope_window(
    scope: Scope,
    dated: set[str],
    date_columns: list[exp.Column],
    values: Mapping[str, QueryParameter],
) -> DateWindow | None:
    select = scope.expression
    where = select.args.get("where") if isinstance(select, exp.Select) else None
    lower: set[date] = set()
    upper: set[date] = set()
    understood: set[int] = set()
    conjuncts = list(where.this.flatten()) if isinstance(where, exp.Where) else []
    for node in conjuncts:
        bound = _bound(node, dated, values)
        if bound is None:
            continue
        column, low, high = bound
        understood.add(id(column))
        lower.update([low] if low is not None else [])
        upper.update([high] if high is not None else [])
    if any(_in_filter(c) and id(c) not in understood for c in date_columns):
        return None
    if len(lower) != 1 or len(upper) != 1:
        return None
    start, end = lower.pop(), upper.pop()
    return DateWindow(start, end) if start <= end else None


def _bound(
    node: exp.Expr, dated: set[str], values: Mapping[str, QueryParameter]
) -> tuple[exp.Column, date | None, date | None] | None:
    """(column, inclusive start, exclusive end) for one date comparison."""
    if isinstance(node, exp.Between):
        column, low, high = node.this, node.args.get("low"), node.args.get("high")
        if not _dated_column(column, dated):
            return None
        start, last = _constant_date(low, values), _constant_date(high, values)
        if start is None or last is None:
            return None
        return column, start, last + timedelta(days=1)
    kind = type(node)
    if kind not in _FLIPPED:
        return None
    left, right = node.args.get("this"), node.args.get("expression")
    if not _dated_column(left, dated):
        left, right, kind = right, left, _FLIPPED[kind]
    if not _dated_column(left, dated):
        return None
    day = _constant_date(right, values)
    if day is None:
        return None
    if kind is exp.EQ:
        return left, day, day + timedelta(days=1)
    if kind in _LOWER:
        return left, day + timedelta(days=_LOWER[kind]), None
    return left, None, day + timedelta(days=_UPPER[kind])


def _dated_column(node: object, dated: set[str]) -> TypeGuard[exp.Column]:
    return (
        isinstance(node, exp.Column)
        and node.name == DATED_FIELD
        and node.table in dated
    )


def _constant_date(node: object, values: Mapping[str, QueryParameter]) -> date | None:
    if isinstance(node, exp.Cast) and node.to.this == exp.DataType.Type.DATE:
        inner = node.this
        if isinstance(inner, exp.Literal) and inner.is_string:
            return _iso_date(inner.this)
        node = inner
    if isinstance(node, exp.Parameter):
        parameter = values.get(node.name)
        if parameter is None:
            return None
        value = parameter.value
        if isinstance(value, date):
            return value
        return _iso_date(value) if isinstance(value, str) else None
    return None


def _iso_date(text: str) -> date | None:
    try:
        return date.fromisoformat(text) if len(text) == 10 else None
    except ValueError:
        return None


# --- value extraction ----------------------------------------------------------------


def _parameterize(tree: exp.Select, offset: int) -> list[QueryParameter]:
    """Move literal values out of the SQL text into typed parameters."""
    parameters: list[QueryParameter] = []

    names: dict[tuple[ParameterType, ScalarValue], str] = {}

    def bind(node: exp.Expr, kind: ParameterType, value: ScalarValue) -> None:
        # Equal literals share a parameter so repeated expressions (SELECT and
        # GROUP BY) stay textually identical.
        name = names.get((kind, value))
        if name is None:
            name = f"_value_{offset + len(parameters)}"
            names[(kind, value)] = name
            parameters.append(QueryParameter(name, kind, value))
        node.replace(exp.Parameter(this=exp.var(name)))

    for literal in list(tree.find_all(exp.Literal)):
        parent = literal.parent
        if _structural_literal(literal, parent):
            continue
        if literal.is_string:
            if (
                isinstance(parent, exp.Cast)
                and parent.to.this == exp.DataType.Type.DATE
            ):
                try:
                    day = date.fromisoformat(literal.this)
                except ValueError:
                    raise reject(
                        ToolErrorCode.INVALID_QUERY,
                        "invalid_literal",
                        "DATE literals must be YYYY-MM-DD",
                    ) from None
                bind(parent, ParameterType.DATE, day)
            else:
                bind(literal, ParameterType.STRING, literal.this)
        elif literal.is_int:
            bind(literal, ParameterType.INT64, int(literal.this))
        else:
            number = float(literal.this)
            if not math.isfinite(number):
                raise reject(
                    ToolErrorCode.INVALID_QUERY, "invalid_literal", "Invalid number"
                )
            bind(literal, ParameterType.FLOAT64, number)
    return parameters


def _structural_literal(literal: exp.Literal, parent: exp.Expr | None) -> bool:
    """Literals that are grammar, not data: LIMIT, date units, interval amounts."""
    if isinstance(parent, exp.Limit | exp.Ordered | exp.Group):
        return True
    if isinstance(parent, exp.DateTrunc) and literal.arg_key == "unit":
        return True
    return (
        isinstance(parent, exp.DateAdd | exp.DateSub)
        and literal.arg_key == "expression"
    )


# --- legacy evidence audit -------------------------------------------------------


class SqlglotGrainAudit:
    """``QueryGrainAudit``: re-runs the demographic grain check on a stored
    logical query (evidence recorded before demographics became
    aggregate-only). The full reviewed catalog is used, so a field that drift
    hides today does not make an old query unverifiable."""

    def __init__(self, catalog: LogicalCatalog | None = None) -> None:
        logical = catalog or default_logical_catalog()
        health = CatalogHealth(
            logical.version, (), frozenset(), frozenset(), MappingProxyType({})
        )
        self._view = build_view(logical, health, entitlement_version=0, visible=True)

    def aggregate_only(self, logical_sql: str) -> bool:
        try:
            statements = sqlglot.parse(
                logical_sql, read=_DIALECT, error_level=ErrorLevel.RAISE
            )
            if len(statements) != 1 or type(statements[0]) is not exp.Select:
                return False
            tree = _qualify(statements[0], self._view)
            scopes = list(traverse_scope(tree))
            _check_columns(tree, scopes, self._view)
            facts = analyze(tree, scopes, self._view)
        except Exception:
            return False
        return not (facts.individual or facts.targeted or facts.identity_outputs)
