"""Compiler contract: authority, parameters, bindings, lineage and output SQL."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from types import MappingProxyType

import duckdb
import pytest
import sqlglot
from sqlglot import exp

from retail_analytics.adapters.sql_compiler import (
    SCOPE_PARAMETER,
    CompilerLimits,
    SqlglotQueryCompiler,
)
from retail_analytics.application.contracts.query_compiler import (
    CompiledQuery,
    FieldRef,
    OutputColumn,
    ParameterType,
    ScalarValue,
)
from retail_analytics.application.ports.query_compiler import QueryCompiler
from retail_analytics.application.query_compiler import (
    DEFAULT_MAXIMUM_BYTES_BILLED,
    QueryRejected,
)
from retail_analytics.domain.access import ProductScope
from retail_analytics.domain.catalog import (
    CatalogHealth,
    CatalogView,
    Derivation,
    FieldType,
    FieldView,
    RelationView,
    SourceColumnRef,
    SourceType,
    build_view,
)
from retail_analytics.domain.logical_catalog import default_logical_catalog
from retail_analytics.domain.operations import ToolErrorCode
from tests.unit.sql_compiler.support import (
    ALICE,
    COMPILER,
    DATASET,
    VERSION,
    compile_sql,
    database,
    execute,
    scope,
    view,
)
from tests.unit.sql_compiler.test_allowed_queries import ALLOWED


@pytest.fixture
def db() -> Iterator[duckdb.DuckDBPyConnection]:
    connection = database()
    yield connection
    connection.close()


def _code(sql: str, **kwargs: object) -> ToolErrorCode:
    with pytest.raises(QueryRejected) as caught:
        compile_sql(sql, **kwargs)  # type: ignore[arg-type]
    return caught.value.code


def test_compiler_satisfies_the_application_port() -> None:
    port: QueryCompiler = COMPILER
    assert port.compile is not None


# --- authority ------------------------------------------------------------------


def test_empty_product_scope_fails_closed() -> None:
    empty = ProductScope(frozenset(), VERSION)
    assert _code("SELECT product_id FROM products", who=empty) is (
        ToolErrorCode.ACCESS_DENIED
    )
    # Even source-free queries: no data access at all without scope.
    assert _code("SELECT 1 AS one", who=empty) is ToolErrorCode.ACCESS_DENIED


def test_invisible_catalog_view_fails_closed() -> None:
    hidden = view(visible=False)
    assert (
        _code("SELECT product_id FROM products", catalog=hidden)
        is ToolErrorCode.ACCESS_DENIED
    )


def test_view_and_scope_must_come_from_the_same_authorization() -> None:
    stale = view(version=VERSION - 1)
    assert (
        _code("SELECT product_id FROM products", catalog=stale)
        is ToolErrorCode.ACCESS_DENIED
    )


@pytest.mark.parametrize("bad", ["01", "abc", "-1", "1.0", "", "1" * 20])
def test_malformed_product_ids_fail_closed(bad: str) -> None:
    who = ProductScope(frozenset({"1", bad}), VERSION)
    assert _code("SELECT product_id FROM products", who=who) is (
        ToolErrorCode.ACCESS_DENIED
    )


def test_scope_parameter_is_trusted_sorted_int_array() -> None:
    compiled = compile_sql("SELECT product_id FROM products", scope(30, 4, 100))
    (policy,) = [p for p in compiled.parameters if p.name == SCOPE_PARAMETER]
    assert policy.trusted and policy.array
    assert policy.type is ParameterType.INT64
    assert policy.value == (4, 30, 100)
    assert policy not in compiled.analysis_parameters


def test_every_physical_read_is_scope_filtered() -> None:
    for query, _ in ALLOWED:
        compiled = compile_sql(query)
        assert_scoped(compiled)


def assert_scoped(compiled: CompiledQuery) -> None:
    """Each SELECT reading a physical table filters by the scope parameter."""
    tree = sqlglot.parse_one(compiled.sql, read="bigquery")
    for select in tree.find_all(exp.Select):
        reads = [
            t
            for t in select.find_all(exp.Table)
            if t.catalog and t.find_ancestor(exp.Select) is select
        ]
        if not reads:
            continue
        assert all((t.catalog, t.db) == tuple(DATASET.split(".")) for t in reads), (
            compiled.sql
        )
        where = select.args.get("where")
        assert where is not None, compiled.sql
        assert SCOPE_PARAMETER in {p.name for p in where.find_all(exp.Parameter)}


# --- analysis values ----------------------------------------------------------------


def test_injection_text_stays_a_bound_value(db: duckdb.DuckDBPyConnection) -> None:
    bad = "Complete' OR 1=1 --"
    compiled = compile_sql(
        "SELECT COUNT(*) AS n FROM sales_items WHERE item_status=@status",
        values={"status": bad},
    )
    assert bad not in compiled.sql
    assert execute(db, compiled) == [(0,)]
    (status,) = compiled.analysis_parameters
    assert (status.name, status.type, status.value) == (
        "status",
        ParameterType.STRING,
        bad,
    )


def test_literals_become_typed_parameters(db: duckdb.DuckDBPyConnection) -> None:
    compiled = compile_sql(
        "SELECT product_id FROM products WHERE product_name='Product A' "
        "AND catalog_price > 1.5 AND product_id < 10 "
        "AND DATE '2026-01-01' < DATE '2026-02-01' ORDER BY product_id LIMIT 1"
    )
    assert "Product A" not in compiled.sql
    assert "2026" not in compiled.sql
    assert "LIMIT 1" in compiled.sql
    kinds = {(p.type, p.value) for p in compiled.analysis_parameters}
    assert kinds == {
        (ParameterType.STRING, "Product A"),
        (ParameterType.FLOAT64, 1.5),
        (ParameterType.INT64, 10),
        (ParameterType.DATE, date(2026, 1, 1)),
        (ParameterType.DATE, date(2026, 2, 1)),
    }
    assert all(p.name.startswith("_value_") for p in compiled.analysis_parameters)
    assert execute(db, compiled) == [(1,)]
    assert "Product A" not in compiled.logical_sql


def test_equal_literals_share_one_parameter() -> None:
    compiled = compile_sql(
        "SELECT SAFE_DIVIDE(sale_amount, 2) AS half FROM sales_items "
        "GROUP BY SAFE_DIVIDE(sale_amount, 2)"
    )
    assert [p.value for p in compiled.analysis_parameters] == [2]


def test_analysis_values_are_typed(db: duckdb.DuckDBPyConnection) -> None:
    values: dict[str, ScalarValue] = {
        "start": date(2026, 9, 1),
        "minimum": Decimal("5.5"),
        "ratio": 0.5,
        "limit_id": 100,
        "flag": True,
    }
    compiled = compile_sql(
        "SELECT SUM(sale_amount) AS total FROM sales_items WHERE ordered_date>=@start "
        "AND sale_amount >= @minimum AND sale_amount * @ratio > 0 "
        "AND product_id < @limit_id AND @flag",
        values=values,
    )
    types = {
        p.name: p.type
        for p in compiled.analysis_parameters
        if not p.name.startswith("_value_")
    }
    assert types == {
        "start": ParameterType.DATE,
        "minimum": ParameterType.NUMERIC,
        "ratio": ParameterType.FLOAT64,
        "limit_id": ParameterType.INT64,
        "flag": ParameterType.BOOL,
    }
    assert execute(db, compiled) == [(50.0,)]


@pytest.mark.parametrize(
    "values",
    [
        {"start": date(2026, 9, 1), SCOPE_PARAMETER: (2,)},
        {"start": date(2026, 9, 1), "_policy_products": 2},
        {"start": date(2026, 9, 1), "_VALUE_0": 1},
        {},
        {"start": date(2026, 9, 1), "extra": 1},
        {"start": datetime(2026, 9, 1, 12)},
        {"start": [date(2026, 9, 1)]},
        {"start": None},
        {"start": float("nan")},
        {"start": Decimal("Infinity")},
        {"start": 2**63},
        {"start": "x" * 1001},
        {"start": b"bytes"},
        {"Start": date(2026, 9, 1), "start": date(2026, 9, 1)},
        {"start date": date(2026, 9, 1)},
    ],
)
def test_bad_analysis_values_are_rejected(values: dict[str, object]) -> None:
    code = _code(
        "SELECT SUM(sale_amount) AS total FROM sales_items WHERE ordered_date>=@start",
        values=values,
    )
    assert code is ToolErrorCode.INVALID_INPUT


# --- bindings and derivations -------------------------------------------------------


def test_only_referenced_fields_are_projected() -> None:
    compiled = compile_sql("SELECT product_id FROM products")
    assert "retail_price" not in compiled.sql and "p.name" not in compiled.sql
    assert compiled.relations == frozenset({"products"})
    assert compiled.fields == frozenset({FieldRef("products", "product_id")})


def test_raw_age_is_read_only_inside_the_age_band_binding() -> None:
    assert "u.age" not in compile_sql("SELECT DISTINCT state FROM customers").sql
    banded = compile_sql("SELECT DISTINCT age_band FROM customers").sql
    assert "u.age" in banded
    for forbidden in ("email", "first_name", "last_name", "street_address", "city"):
        assert forbidden not in banded


def test_privacy_derivations_fail_closed_without_an_implementation() -> None:
    plain = SqlglotQueryCompiler(DATASET)
    for query in (
        "SELECT customer_ref FROM customers",
        "SELECT age_band, COUNT(*) AS n FROM customers GROUP BY age_band",
        "SELECT COUNT(DISTINCT order_ref) AS n FROM sales_items",
        "SELECT s.sale_amount FROM sales_items s JOIN orders o "
        "ON s.order_ref = o.order_ref",
    ):
        with pytest.raises(QueryRejected) as caught:
            compile_sql(query, compiler=plain)
        assert caught.value.code is ToolErrorCode.FIELD_UNAVAILABLE
        assert caught.value.reason == "derivation_unavailable"
    # Queries that need no privacy derivation still work.
    compile_sql("SELECT SUM(sale_amount) AS t FROM sales_items", compiler=plain)
    compile_sql(
        "SELECT state, COUNT(*) AS n FROM customers GROUP BY state", compiler=plain
    )


def _with_relation(relation: RelationView) -> CatalogView:
    base = view()
    relations = dict(base.relations)
    relations[relation.name] = relation
    return replace(base, relations=MappingProxyType(relations))


def test_binding_rechecks_forbidden_source_columns() -> None:
    customers = view().relations["customers"]
    leaked = FieldView(
        "contact",
        FieldType.STRING,
        "Unreviewed field smuggled into a view.",
        Derivation.DIRECT,
        (SourceColumnRef("users", "email", frozenset({SourceType.STRING})),),
    )
    raw_age = FieldView(
        "years",
        FieldType.INTEGER,
        "Exact age without a band.",
        Derivation.DIRECT,
        (SourceColumnRef("users", "age", frozenset({SourceType.INT64})),),
    )
    for field in (leaked, raw_age):
        tampered = _with_relation(replace(customers, fields=(*customers.fields, field)))
        with pytest.raises(QueryRejected) as caught:
            compile_sql(f"SELECT {field.name} FROM customers", catalog=tampered)
        assert caught.value.reason == "binding_unavailable"


def test_relation_without_a_trusted_binding_is_unavailable() -> None:
    extra = RelationView("customer_segments", "x", "x", (), ())
    with pytest.raises(QueryRejected) as caught:
        compile_sql(
            "SELECT COUNT(*) AS n FROM customer_segments", catalog=_with_relation(extra)
        )
    assert caught.value.reason == "relation_unavailable"


def test_drift_disabled_fields_and_relations_are_unavailable() -> None:
    catalog = default_logical_catalog()
    health = CatalogHealth(
        catalog.version,
        (),
        frozenset({("products", "brand")}),
        frozenset({"orders"}),
        {},
    )
    drifted = build_view(catalog, health, entitlement_version=VERSION, visible=True)
    assert _code("SELECT brand FROM products", catalog=drifted) is (
        ToolErrorCode.FIELD_UNAVAILABLE
    )
    assert _code("SELECT order_ref FROM orders", catalog=drifted) is (
        ToolErrorCode.FIELD_UNAVAILABLE
    )
    compile_sql("SELECT category FROM products", catalog=drifted)


def test_derivation_parameters_must_be_trusted_policy_names() -> None:
    class Leaky:
        def opaque_reference(self, kind: str, raw_key: exp.Expr) -> exp.Expr:
            return raw_key

        def age_band(self, raw_age: exp.Expr) -> exp.Expr:
            return raw_age

        def parameters(self) -> tuple[object, ...]:
            from retail_analytics.application.contracts.query_compiler import (
                QueryParameter,
            )

            return (QueryParameter("key", ParameterType.STRING, "k"),)

    with pytest.raises(ValueError, match="trusted"):
        SqlglotQueryCompiler(DATASET, derivations=Leaky())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "dataset", ["users", "a.b.c", "`x`.y", "proj.data set", "PROJ.ds", "p.d;DROP"]
)
def test_dataset_configuration_is_validated(dataset: str) -> None:
    with pytest.raises(ValueError):
        SqlglotQueryCompiler(dataset)


# --- output, lineage and limits -----------------------------------------------------


def test_output_lineage_follows_ctes_and_subqueries() -> None:
    compiled = compile_sql(
        "WITH b AS (SELECT c.age_band AS band, s.sale_amount AS v FROM sales_items s "
        "JOIN customers c ON s.customer_ref = c.customer_ref) "
        "SELECT band, SUM(v) AS total, (SELECT COUNT(*) FROM products) AS n "
        "FROM b GROUP BY band"
    )
    assert compiled.outputs == (
        OutputColumn("band", frozenset({FieldRef("customers", "age_band")}), True),
        OutputColumn(
            "total", frozenset({FieldRef("sales_items", "sale_amount")}), False
        ),
        OutputColumn("n", frozenset(), False),
    )
    assert compiled.relations == frozenset({"sales_items", "customers", "products"})
    assert FieldRef("sales_items", "customer_ref") in compiled.fields


def test_alias_named_like_pii_keeps_true_lineage() -> None:
    compiled = compile_sql("SELECT sale_amount AS email FROM sales_items")
    assert compiled.outputs == (
        OutputColumn(
            "email", frozenset({FieldRef("sales_items", "sale_amount")}), True
        ),
    )


def test_comments_never_reach_the_compiled_sql() -> None:
    compiled = compile_sql(
        "SELECT product_id /* note */ FROM products -- trailing\nWHERE product_id > 0"
    )
    assert "/*" not in compiled.sql and "--" not in compiled.sql
    assert "note" not in compiled.logical_sql


def test_logical_sql_uses_only_logical_names() -> None:
    compiled = compile_sql("SELECT SUM(sale_amount) AS t FROM sales_items")
    assert "thelook" not in compiled.logical_sql
    assert "sales_items" in compiled.logical_sql
    assert "thelook" in compiled.sql


def test_byte_budget_is_carried_for_the_executor() -> None:
    assert compile_sql("SELECT 1 AS one").maximum_bytes_billed == (
        DEFAULT_MAXIMUM_BYTES_BILLED
    )
    small = SqlglotQueryCompiler(
        DATASET, limits=CompilerLimits(maximum_bytes_billed=10 * 1024**2)
    )
    assert compile_sql("SELECT 1 AS one", compiler=small).maximum_bytes_billed == (
        10 * 1024**2
    )


def test_size_limits_are_configurable() -> None:
    tight = SqlglotQueryCompiler(DATASET, limits=CompilerLimits(max_sql_chars=20))
    with pytest.raises(QueryRejected) as caught:
        compile_sql("SELECT product_id FROM products", compiler=tight)
    assert caught.value.reason == "query_too_large"


def test_rejection_repr_has_no_message_detail() -> None:
    with pytest.raises(QueryRejected) as caught:
        compile_sql("SELECT email FROM customers")
    assert "email" not in repr(caught.value)
    assert caught.value.correctable
    with pytest.raises(QueryRejected) as denied:
        compile_sql("SELECT 1 AS x", who=ProductScope(frozenset(), VERSION))
    assert not denied.value.correctable


def test_compiled_sql_is_one_bigquery_select_with_named_parameters() -> None:
    compiled = compile_sql(
        "SELECT product_id FROM products WHERE product_name = @name",
        values={"name": "A"},
    )
    tree = sqlglot.parse_one(compiled.sql, read="bigquery")
    assert isinstance(tree, exp.Select)
    referenced = {p.name for p in tree.find_all(exp.Parameter)}
    assert referenced == {p.name for p in compiled.parameters}
    assert ALICE.entitlement_version == compiled.entitlement_version
