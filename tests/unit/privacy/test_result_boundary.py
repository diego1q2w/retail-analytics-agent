"""The result privacy boundary on its own: fail-closed checks, masking and bounds."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import date
from decimal import Decimal
from types import MappingProxyType

import pytest

from retail_analytics.application.query_compiler import CompiledQuery
from retail_analytics.application.result_privacy import (
    MASK,
    ColumnRole,
    QueryRows,
    ReleasedResult,
    ResultLimits,
    ResultPrivacyBoundary,
    ResultWithheld,
    TruncationReason,
)
from retail_analytics.domain.catalog import (
    CatalogView,
    Derivation,
    FieldType,
    FieldView,
    SourceColumnRef,
    SourceType,
)
from retail_analytics.domain.operations import ToolErrorCode
from tests.unit.privacy.support import BOUNDARY, EXEC_A, SCOPE_A, compile_for, ref
from tests.unit.sql_compiler.support import view

DETAIL = "SELECT customer_ref, state, age_band FROM customers"
CUSTOMER = ref(EXEC_A, "customer_ref", 10)


def _compiled(sql: str = DETAIL) -> CompiledQuery:
    return compile_for(EXEC_A, sql, SCOPE_A)


def _release(
    rows: Sequence[Sequence[object]],
    sql: str = DETAIL,
    *,
    catalog: CatalogView | None = None,
    complete: bool = True,
) -> ReleasedResult:
    compiled = _compiled(sql)
    names = tuple(o.name for o in compiled.outputs)
    return BOUNDARY.release(
        compiled, QueryRows(names, rows, complete), catalog=catalog or view()
    )


def _withheld(
    code: ToolErrorCode,
    reason: str,
    rows: Sequence[Sequence[object]],
    sql: str = DETAIL,
    *,
    catalog: CatalogView | None = None,
) -> None:
    with pytest.raises(ResultWithheld) as caught:
        _release(rows, sql, catalog=catalog)
    assert (caught.value.code, caught.value.reason) == (code, reason)
    # Safe messages never echo values.
    assert CUSTOMER not in caught.value.message


def test_valid_rows_are_released_with_roles() -> None:
    result = _release([(CUSTOMER, "CA", "25-29"), (None, None, None)])
    assert result.rows == ((CUSTOMER, "CA", "25-29"), (None, None, None))
    assert [c.role for c in result.columns] == [
        ColumnRole.REFERENCE,
        ColumnRole.VALUE,
        ColumnRole.AGE_BAND,
    ]
    assert result.records()[0] == {
        "customer_ref": CUSTOMER,
        "state": "CA",
        "age_band": "25-29",
    }
    assert result.policy_version == 1
    assert "CA" not in repr(result)


@pytest.mark.parametrize("leak", ["10", 10, "ord_" + "a" * 24, "cus_123", "Alice"])
def test_reference_column_with_a_non_reference_is_withheld(leak: object) -> None:
    _withheld(ToolErrorCode.INTERNAL_ERROR, "invalid_reference", [(leak, "CA", None)])


@pytest.mark.parametrize("leak", ["27", 27, "25-28", "26-30", "85+", "under 30"])
def test_age_band_column_with_an_exact_or_off_grid_age_is_withheld(
    leak: object,
) -> None:
    _withheld(ToolErrorCode.INTERNAL_ERROR, "invalid_age_band", [(None, "CA", leak)])


def test_reference_check_follows_lineage_through_aliases_and_ctes() -> None:
    sql = (
        "WITH x AS (SELECT customer_ref AS who FROM customers) "
        "SELECT who AS anyone FROM x"
    )
    _withheld(ToolErrorCode.INTERNAL_ERROR, "invalid_reference", [("10",)], sql)
    assert _release([(CUSTOMER,)], sql).columns[0].role is ColumnRole.REFERENCE


@pytest.mark.parametrize(
    "value",
    [
        "alice@example.invalid",
        "Call +1 415 555 0100 now",
        "ship to 12 Main Street",
    ],
)
def test_free_text_that_looks_like_personal_data_is_masked(value: str) -> None:
    result = _release(
        [(1, value), (2, "Jeans")],
        "SELECT product_id, product_name FROM products",
    )
    assert result.rows == ((1, MASK), (2, "Jeans"))
    assert result.masked_cells == 1
    assert result.masked_columns == ("product_name",)


@pytest.mark.parametrize("value", [b"raw", [1], {"a": 1}, object()])
def test_non_scalar_values_are_withheld(value: object) -> None:
    _withheld(
        ToolErrorCode.INTERNAL_ERROR,
        "unsupported_value",
        [(1, value)],
        "SELECT product_id, product_name FROM products",
    )


def test_scalar_types_pass_and_nan_becomes_null() -> None:
    result = _release(
        [(1, Decimal("1.50"), date(2026, 9, 1), float("nan"))],
        "SELECT product_id, catalog_price, DATE '2026-09-01' AS d, "
        "SAFE_DIVIDE(catalog_price, 0) AS r FROM products",
    )
    assert result.rows == ((1, Decimal("1.50"), date(2026, 9, 1), None),)


def test_result_shape_must_match_the_compiled_outputs() -> None:
    compiled = _compiled()
    for columns in (("customer_ref", "state"), ("state", "customer_ref", "age_band")):
        with pytest.raises(ResultWithheld) as caught:
            BOUNDARY.release(compiled, QueryRows(columns, []), catalog=view())
        assert caught.value.reason == "shape_mismatch"
    _withheld(ToolErrorCode.INTERNAL_ERROR, "shape_mismatch", [(CUSTOMER, "CA")])
    _withheld(ToolErrorCode.INTERNAL_ERROR, "shape_mismatch", ["abc"])


def test_stale_authorization_withholds_computed_rows() -> None:
    newer = view(version=SCOPE_A.entitlement_version + 1)
    _withheld(
        ToolErrorCode.ACCESS_DENIED,
        "stale_authorization",
        [(CUSTOMER, "CA", None)],
        catalog=newer,
    )


def test_revoked_access_withholds_computed_rows() -> None:
    _withheld(
        ToolErrorCode.ACCESS_DENIED,
        "no_product_scope",
        [(CUSTOMER, "CA", None)],
        catalog=view(visible=False),
    )


def _without_field(catalog: CatalogView, relation: str, field: str) -> CatalogView:
    rel = catalog.relations[relation]
    fields = tuple(f for f in rel.fields if f.name != field)
    relations = dict(catalog.relations)
    relations[relation] = replace(rel, fields=fields)
    return replace(catalog, relations=MappingProxyType(relations))


def test_withdrawn_field_withholds_rows_even_if_unselected() -> None:
    # age_band only filters here, yet its withdrawal still blocks release.
    sql = "SELECT state FROM customers WHERE age_band = '25-29'"
    _withheld(
        ToolErrorCode.FIELD_UNAVAILABLE,
        "catalog_changed",
        [("CA",)],
        sql,
        catalog=_without_field(view(), "customers", "age_band"),
    )


def test_field_with_a_forbidden_source_is_withheld() -> None:
    # Defense in depth: a catalog entry that maps exact age directly.
    catalog = view()
    rel = catalog.relations["customers"]
    leaky = FieldView(
        "state",
        FieldType.STRING,
        "tampered",
        Derivation.DIRECT,
        (SourceColumnRef("users", "age", frozenset({SourceType.INT64})),),
    )
    fields = tuple(leaky if f.name == "state" else f for f in rel.fields)
    tampered = replace(
        catalog,
        relations=MappingProxyType(
            {**catalog.relations, "customers": replace(rel, fields=fields)}
        ),
    )
    _withheld(
        ToolErrorCode.INTERNAL_ERROR,
        "forbidden_source",
        [(CUSTOMER, "CA", None)],
        catalog=tampered,
    )


def test_row_limit_truncates_truthfully() -> None:
    boundary = ResultPrivacyBoundary(ResultLimits(max_rows=2))
    compiled = _compiled("SELECT product_id FROM products")
    result = boundary.release(
        compiled, QueryRows(("product_id",), [(1,), (2,), (3,)]), catalog=view()
    )
    assert result.rows == ((1,), (2,))
    assert result.truncation is TruncationReason.ROWS and result.truncated
    assert result.received_rows == 3


def test_byte_limit_truncates_truthfully() -> None:
    boundary = ResultPrivacyBoundary(ResultLimits(max_bytes=1024))
    compiled = _compiled("SELECT product_name FROM products")
    rows = [("x" * 300,) for _ in range(10)]
    result = boundary.release(
        compiled, QueryRows(("product_name",), rows), catalog=view()
    )
    assert 0 < len(result.rows) < 10
    assert result.truncation is TruncationReason.BYTES


def test_incomplete_source_read_is_reported() -> None:
    result = _release([(CUSTOMER, "CA", None)], complete=False)
    assert result.truncation is TruncationReason.SOURCE


def test_masks_in_truncated_rows_are_not_counted() -> None:
    boundary = ResultPrivacyBoundary(ResultLimits(max_rows=1))
    compiled = _compiled("SELECT product_name FROM products")
    result = boundary.release(
        compiled,
        QueryRows(("product_name",), [("ok",), ("a@b.example",)]),
        catalog=view(),
    )
    assert result.masked_cells == 0


def test_default_limits_match_operational_defaults() -> None:
    limits = ResultLimits()
    assert (limits.max_rows, limits.max_bytes) == (500, 256 * 1024)
    with pytest.raises(ValueError):
        ResultLimits(max_rows=0)


def test_raw_rows_repr_does_not_print_values() -> None:
    rows = QueryRows(("customer_ref",), [(CUSTOMER,)])
    assert CUSTOMER not in repr(rows)
