"""Shared fixtures for compiler tests: catalog views, scopes and a DuckDB oracle.

DuckDB runs the compiled statement (transpiled from BigQuery SQL) over small
synthetic copies of the four source tables. It is a result oracle for scope
and privacy behavior, not proof of BigQuery semantics; live dry runs cover
engine validity.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import duckdb
import sqlglot
from sqlglot import exp

from retail_analytics.adapters.sql_compiler import SqlglotQueryCompiler
from retail_analytics.application.contracts.query_compiler import (
    AnalysisQuery,
    CompiledQuery,
    ParameterType,
    QueryParameter,
    ScalarValue,
)
from retail_analytics.domain.access import ProductScope
from retail_analytics.domain.catalog import (
    CatalogHealth,
    CatalogView,
    build_view,
)
from retail_analytics.domain.logical_catalog import default_logical_catalog

DATASET = "bigquery-public-data.thelook_ecommerce"
RAW = '"bigquery-public-data".thelook_ecommerce.'
VERSION = 7


class FixtureDerivations:
    """Test-only stand-ins for the privacy-owned derivations.

    References here are transparent (``ref-customer_ref-10``) so expected
    results are readable; they are NOT opaque and never ship. The salt
    parameter exercises the trusted-parameter seam.
    """

    def opaque_reference(self, kind: str, raw_key: exp.Expr) -> exp.Expr:
        return exp.func(
            "CONCAT",
            exp.Parameter(this=exp.var("_policy_ref_salt")),
            exp.Literal.string(f"{kind}-"),
            exp.cast(raw_key, exp.DataType.Type.TEXT),
        )

    def age_band(self, raw_age: exp.Expr) -> exp.Expr:
        return sqlglot.parse_one(
            "CASE WHEN a < 30 THEN 'under 30' WHEN a < 50 THEN '30-49' ELSE '50+' END",
            read="bigquery",
        ).transform(lambda n: raw_age.copy() if isinstance(n, exp.Column) else n)

    def parameters(self) -> tuple[QueryParameter, ...]:
        return (
            QueryParameter(
                "_policy_ref_salt", ParameterType.STRING, "ref-", trusted=True
            ),
        )


def view(*, version: int = VERSION, visible: bool = True) -> CatalogView:
    catalog = default_logical_catalog()
    health = CatalogHealth(catalog.version, (), frozenset(), frozenset(), {})
    return build_view(catalog, health, entitlement_version=version, visible=visible)


def scope(*products: int, version: int = VERSION) -> ProductScope:
    return ProductScope(frozenset(str(p) for p in products), version)


ALICE = scope(1, 3)
BOB = scope(2)
COMPILER = SqlglotQueryCompiler(DATASET, derivations=FixtureDerivations())


def compile_sql(
    sql: str,
    who: ProductScope = ALICE,
    values: Mapping[str, ScalarValue] | None = None,
    *,
    compiler: SqlglotQueryCompiler = COMPILER,
    catalog: CatalogView | None = None,
) -> CompiledQuery:
    return compiler.compile(
        AnalysisQuery(sql, dict(values or {})),
        catalog=catalog or view(version=who.entitlement_version),
        scope=who,
    )


def database() -> duckdb.DuckDBPyConnection:
    c = duckdb.connect(":memory:")
    c.execute("ATTACH ':memory:' AS \"bigquery-public-data\"")
    c.execute('CREATE SCHEMA "bigquery-public-data".thelook_ecommerce')
    c.execute(
        f"CREATE TABLE {RAW}products(id BIGINT PRIMARY KEY, name VARCHAR, "
        "category VARCHAR, brand VARCHAR, department VARCHAR, "
        "retail_price DOUBLE, cost DOUBLE)"
    )
    c.execute(f"""INSERT INTO {RAW}products VALUES
        (1,'Product A','Clothing','Alpha','Women',30,10),
        (2,'Product B','Private category','Beta','Men',70,20),
        (3,'Unsold allowed product','Clothing','Alpha','Women',15,5)""")
    c.execute(
        f"CREATE TABLE {RAW}users(id BIGINT PRIMARY KEY, first_name VARCHAR, "
        "last_name VARCHAR, email VARCHAR, age BIGINT, street_address VARCHAR, "
        "city VARCHAR, state VARCHAR, country VARCHAR)"
    )
    c.execute(f"""INSERT INTO {RAW}users VALUES
        (10,'Alice','Private','alice@example.invalid',27,'1 Main St',
         'Springfield','CA','US'),
        (20,'Bob','Secret','bob@example.invalid',41,'2 High St','Albany','NY','US')""")
    c.execute(
        f"CREATE TABLE {RAW}orders(order_id BIGINT PRIMARY KEY, user_id BIGINT, "
        "created_at TIMESTAMP, num_of_item BIGINT, status VARCHAR)"
    )
    c.execute(f"""INSERT INTO {RAW}orders VALUES
        (100,10,'2026-09-10 23:30:00',2,'Complete'),
        (101,10,'2026-09-12 08:00:00',1,'Shipped'),
        (102,20,'2026-09-14 12:00:00',1,'Complete'),
        (103,10,'2026-08-05 09:00:00',1,'Complete')""")
    c.execute(
        f"CREATE TABLE {RAW}order_items(id BIGINT PRIMARY KEY, order_id BIGINT, "
        "user_id BIGINT, product_id BIGINT, status VARCHAR, sale_price DOUBLE)"
    )
    c.execute(f"""INSERT INTO {RAW}order_items VALUES
        (1000,100,10,1,'Complete',30),
        (1001,100,10,2,'Complete',70),
        (1002,101,10,1,'Shipped',20),
        (1003,102,20,2,'Complete',90),
        (1004,103,10,1,'Complete',10)""")
    return c


def duckdb_sql(compiled: CompiledQuery) -> str:
    (sql,) = sqlglot.transpile(compiled.sql, read="bigquery", write="duckdb")
    return sql


def execute(db: duckdb.DuckDBPyConnection, compiled: CompiledQuery) -> list[Any]:
    sql = duckdb_sql(compiled)
    params = {
        p.name: list(p.value) if isinstance(p.value, tuple) else p.value
        for p in compiled.parameters
        if f"${p.name}" in sql
    }
    return db.execute(sql, params).fetchall()


def run(
    db: duckdb.DuckDBPyConnection,
    sql: str,
    who: ProductScope = ALICE,
    values: Mapping[str, ScalarValue] | None = None,
) -> Sequence[Any]:
    return execute(db, compile_sql(sql, who, values))
