"""The supported SQL subset, as an explicit allowlist over SQLGlot nodes.

SQLGlot parses far more than we accept. Every node type and every populated
argument of that node must appear in ``_ALLOWED``; anything else (window
functions, set operations, UNNEST, table functions, unknown or user-defined
functions, scripting, DML/DDL, system variables...) fails closed. Adding a
construct means adding it here together with positive and adversarial tests.
"""

from __future__ import annotations

from collections.abc import Callable

from sqlglot import exp

from retail_analytics.adapters.sql_compiler.errors import reject, unsupported
from retail_analytics.application.query_compiler import RESERVED_PREFIXES
from retail_analytics.domain.operations import ToolErrorCode

_BINARY = frozenset({"this", "expression"})
_UNARY = frozenset({"this"})

# node type -> arguments that may be populated
_ALLOWED: dict[type[exp.Expr], frozenset[str]] = {
    # query structure
    exp.Select: frozenset(
        {
            "expressions",
            "from_",
            "where",
            "group",
            "having",
            "order",
            "limit",
            "distinct",
            "with_",
            "joins",
        }
    ),
    exp.From: _UNARY,
    exp.Where: _UNARY,
    exp.Having: _UNARY,
    exp.Group: frozenset({"expressions"}),
    exp.Order: frozenset({"expressions"}),
    exp.Ordered: frozenset({"this", "desc", "nulls_first"}),
    exp.Limit: frozenset({"expression"}),
    exp.With: frozenset({"expressions"}),
    exp.CTE: frozenset({"this", "alias"}),
    exp.Table: frozenset({"this", "alias"}),
    exp.TableAlias: _UNARY,
    exp.Subquery: frozenset({"this", "alias"}),
    exp.Join: frozenset({"this", "on", "side", "kind"}),
    exp.Distinct: frozenset({"expressions"}),
    # references and values
    exp.Column: frozenset({"this", "table"}),
    exp.Identifier: frozenset({"this", "quoted"}),
    exp.Literal: frozenset({"this", "is_string"}),
    exp.Parameter: _UNARY,
    exp.Var: _UNARY,
    exp.Null: frozenset(),
    exp.Boolean: _UNARY,
    exp.Star: frozenset(),
    exp.Alias: frozenset({"this", "alias"}),
    exp.Paren: _UNARY,
    # predicates and arithmetic (no plain division: use SAFE_DIVIDE)
    exp.And: _BINARY,
    exp.Or: _BINARY,
    exp.Not: _UNARY,
    exp.EQ: _BINARY,
    exp.NEQ: _BINARY,
    exp.GT: _BINARY,
    exp.GTE: _BINARY,
    exp.LT: _BINARY,
    exp.LTE: _BINARY,
    exp.Is: _BINARY,
    exp.Between: frozenset({"this", "low", "high"}),
    exp.In: frozenset({"this", "expressions", "query"}),
    exp.Like: frozenset({"this", "expression", "negate"}),
    exp.Exists: _UNARY,
    exp.Add: _BINARY,
    exp.Sub: _BINARY,
    exp.Mul: _BINARY,
    exp.Neg: _UNARY,
    exp.Case: frozenset({"this", "ifs", "default"}),
    exp.If: frozenset({"this", "true", "false"}),
    # functions
    exp.Sum: _UNARY,
    exp.Avg: _UNARY,
    exp.Min: _UNARY,
    exp.Max: _UNARY,
    exp.Count: frozenset({"this", "expressions", "big_int"}),
    exp.CountIf: _UNARY,
    exp.Coalesce: frozenset({"this", "expressions"}),
    exp.Nullif: _BINARY,
    exp.SafeDivide: _BINARY,
    exp.Abs: _UNARY,
    exp.Round: frozenset({"this", "decimals"}),
    exp.Lower: _UNARY,
    exp.Upper: _UNARY,
    exp.DateTrunc: frozenset({"this", "unit"}),
    exp.Extract: _BINARY,
    exp.DateDiff: frozenset({"this", "expression", "unit", "date_part_boundary"}),
    exp.DateAdd: frozenset({"this", "expression", "unit"}),
    exp.DateSub: frozenset({"this", "expression", "unit"}),
    exp.Cast: frozenset({"this", "to"}),
    exp.DataType: frozenset({"this", "nested"}),
}

_CAST_TYPES = frozenset(
    {
        exp.DataType.Type.DATE,
        exp.DataType.Type.BIGINT,
        exp.DataType.Type.INT,
        exp.DataType.Type.DECIMAL,
        exp.DataType.Type.DOUBLE,
        exp.DataType.Type.TEXT,
    }
)
_TRUNC_UNITS = frozenset({"DAY", "WEEK", "MONTH", "QUARTER", "YEAR"})
_EXTRACT_UNITS = frozenset({"DAYOFWEEK", "DAY", "WEEK", "MONTH", "QUARTER", "YEAR"})
_DATE_ARITHMETIC_UNITS = frozenset({"DAY", "WEEK", "MONTH", "QUARTER", "YEAR"})
_MAX_INTERVAL = 10_000
MAX_LIMIT = 10_000


def _populated(node: exp.Expr) -> set[str]:
    return {
        k for k, v in node.args.items() if v is not None and v is not False and v != []
    }


def check_grammar(tree: exp.Expr) -> None:
    """Reject anything outside the subset. Also strips comments in place."""
    for node in tree.walk():
        allowed = _ALLOWED.get(type(node))
        if allowed is None:
            raise unsupported(
                "unsupported_construct",
                f"{_label(node)} is not supported in analytical queries",
            )
        if _populated(node) - allowed:
            raise unsupported(
                "unsupported_construct",
                f"This form of {_label(node)} is not supported",
            )
        check = _NODE_CHECKS.get(type(node))
        if check is not None:
            check(node)
        node.comments = None


def _label(node: exp.Expr) -> str:
    if isinstance(node, exp.Func):
        return f"Function {node.sql_name()}"
    return f"Construct {type(node).__name__}"


def is_reserved(name: str) -> bool:
    return name.casefold().startswith(RESERVED_PREFIXES)


def _identifier(node: exp.Expr) -> None:
    if is_reserved(node.name):
        raise reject(
            ToolErrorCode.INVALID_QUERY,
            "reserved_name",
            "Names starting with _policy_ or _value_ are reserved",
        )


def _select(node: exp.Expr) -> None:
    if not node.expressions:
        raise reject(
            ToolErrorCode.INVALID_QUERY,
            "empty_select",
            "A SELECT must list explicit expressions",
        )
    distinct = node.args.get("distinct")
    if distinct is not None and distinct.expressions:
        raise unsupported(
            "unsupported_construct", "SELECT DISTINCT ON is not supported"
        )


def _star(node: exp.Expr) -> None:
    parent = node.parent
    if not (type(parent) is exp.Count and parent.this is node):
        raise reject(
            ToolErrorCode.UNSUPPORTED_SQL,
            "wildcard_projection",
            "Select explicit fields instead of a wildcard; COUNT(*) is allowed",
        )


def _table(node: exp.Expr) -> None:
    if type(node.this) is not exp.Identifier:
        raise unsupported(
            "unsupported_source", "Only named logical relations can be queried"
        )


def _parameter(node: exp.Expr) -> None:
    # ``@name`` only: nested parameters are system variables (``@@project_id``).
    if type(node.this) is not exp.Var or not node.name.isidentifier():
        raise unsupported("unsupported_parameter", "Use named parameters like @name")


def _data_type(node: exp.Expr) -> None:
    if node.this not in _CAST_TYPES or node.args.get("expressions"):
        raise unsupported(
            "unsupported_type",
            "CAST supports STRING, INT64, NUMERIC, FLOAT64 and DATE",
        )


def _unit_name(unit: exp.Expr | None) -> str:
    if isinstance(unit, exp.Var) or (isinstance(unit, exp.Literal) and unit.is_string):
        return unit.name.upper()
    return ""


def _date_trunc(node: exp.Expr) -> None:
    if _unit_name(node.args.get("unit")) not in _TRUNC_UNITS:
        raise unsupported(
            "unsupported_date_unit",
            "DATE_TRUNC supports DAY, WEEK, MONTH, QUARTER, YEAR",
        )


def _extract(node: exp.Expr) -> None:
    if _unit_name(node.this) not in _EXTRACT_UNITS:
        raise unsupported("unsupported_date_unit", "Unsupported EXTRACT part")


def _date_diff(node: exp.Expr) -> None:
    if _unit_name(node.args.get("unit")) not in _DATE_ARITHMETIC_UNITS:
        raise unsupported("unsupported_date_unit", "Unsupported DATE_DIFF part")


def _date_shift(node: exp.Expr) -> None:
    if _unit_name(node.args.get("unit")) not in _DATE_ARITHMETIC_UNITS:
        raise unsupported("unsupported_date_unit", "Unsupported interval unit")
    amount = node.expression
    text = amount.name if isinstance(amount, exp.Literal) else ""
    digits = text[1:] if text.startswith("-") else text
    if not (digits.isascii() and digits.isdigit() and abs(int(text)) <= _MAX_INTERVAL):
        raise unsupported(
            "unsupported_interval", "INTERVAL needs an integer literal amount"
        )


def _limit(node: exp.Expr) -> None:
    value = node.expression
    if not (
        isinstance(value, exp.Literal)
        and value.is_int
        and 0 <= int(value.this) <= MAX_LIMIT
    ):
        raise reject(
            ToolErrorCode.INVALID_QUERY,
            "invalid_limit",
            f"LIMIT must be an integer literal from 0 to {MAX_LIMIT}",
        )


def _literal(node: exp.Expr) -> None:
    if node.is_string:
        return
    text = node.this
    if not isinstance(text, str) or not text.isascii():
        raise reject(ToolErrorCode.INVALID_QUERY, "invalid_literal", "Invalid number")
    if node.is_int and not -(2**63) <= int(text) < 2**63:
        raise reject(
            ToolErrorCode.INVALID_QUERY, "invalid_literal", "Integer out of range"
        )


def _count(node: exp.Expr) -> None:
    if node.expressions:
        raise unsupported("unsupported_construct", "COUNT takes one argument")


_NODE_CHECKS: dict[type[exp.Expr], Callable[[exp.Expr], None]] = {
    exp.Identifier: _identifier,
    exp.Var: _identifier,
    exp.Select: _select,
    exp.Star: _star,
    exp.Table: _table,
    exp.Parameter: _parameter,
    exp.DataType: _data_type,
    exp.DateTrunc: _date_trunc,
    exp.Extract: _extract,
    exp.DateDiff: _date_diff,
    exp.DateAdd: _date_shift,
    exp.DateSub: _date_shift,
    exp.Limit: _limit,
    exp.Literal: _literal,
    exp.Count: _count,
}
