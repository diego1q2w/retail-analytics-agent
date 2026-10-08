"""Fixtures for the customer-privacy suite: real derivations over the DuckDB oracle.

Unlike the compiler suite (transparent test references), these tests compile
with the production :class:`KeyedDerivations`, so references are real keyed
HMACs and age bands come from the real grid.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import duckdb

from retail_analytics.adapters.sql_compiler import (
    ReferenceKeyring,
    ScopedSqlglotCompilers,
)
from retail_analytics.application.query_compiler import (
    AnalysisQuery,
    CompiledQuery,
    ScalarValue,
)
from retail_analytics.application.result_privacy import (
    QueryRows,
    ReleasedResult,
    ResultPrivacyBoundary,
)
from retail_analytics.domain.access import ProductScope
from tests.unit.sql_compiler.support import (
    DATASET,
    RAW,
    database,
    duckdb_sql,
    scope,
    view,
)

# Test-only key material (never a real secret).
MASTER_KEY = b"test-only-reference-master-key-0123456789"
KEYRING = ReferenceKeyring(MASTER_KEY)
COMPILERS = ScopedSqlglotCompilers(DATASET, KEYRING)
BOUNDARY = ResultPrivacyBoundary()

EXEC_A = "demo-a"
EXEC_B = "demo-b"
# Executive A sees products 1 and 3; B sees product 2.
SCOPE_A = scope(1, 3)
SCOPE_B = scope(2)

# Personal data planted in the fixture; none of it may ever be released.
PII_STRINGS = (
    "Alice",
    "Bob",
    "Carol",
    "Dave",
    "Erin",
    "Private",
    "Secret",
    "example.invalid",
    "Main St",
    "High St",
    "Springfield",
    "Albany",
)
# user id -> exact age
AGES = {10: 27, 20: 41, 30: 29, 40: 93, 50: None}


def customer_database() -> duckdb.DuckDBPyConnection:
    """The compiler fixture plus customers forming tiny and shared cohorts."""
    db = database()
    db.execute(f"""INSERT INTO {RAW}users VALUES
        (30,'Carol','Private','carol@example.invalid',29,'3 Main St',
         'Springfield','CA','US'),
        (40,'Dave','Secret','dave@example.invalid',93,'4 High St','Austin','TX','US'),
        (50,'Erin','Private','erin@example.invalid',NULL,'5 Main St',
         'Albany','NY','US')""")
    db.execute(f"""INSERT INTO {RAW}orders VALUES
        (104,30,'2026-09-15 10:00:00',1,'Complete'),
        (105,40,'2026-09-16 10:00:00',1,'Complete'),
        (106,50,'2026-09-17 10:00:00',1,'Complete'),
        (107,30,'2026-09-18 10:00:00',1,'Complete')""")
    db.execute(f"""INSERT INTO {RAW}order_items VALUES
        (1005,104,30,1,'Complete',45),
        (1006,105,40,3,'Complete',12),
        (1007,106,50,1,'Complete',5),
        (1008,107,30,2,'Complete',60)""")
    return db


def set_age(db: duckdb.DuckDBPyConnection, user_id: int, age: int | None) -> None:
    db.execute(f"UPDATE {RAW}users SET age = ? WHERE id = ?", [age, user_id])


def compile_for(
    executive: str,
    sql: str,
    who: ProductScope,
    values: Mapping[str, ScalarValue] | None = None,
    *,
    compilers: ScopedSqlglotCompilers = COMPILERS,
) -> CompiledQuery:
    return compilers.for_executive(executive).compile(
        AnalysisQuery(sql, dict(values or {})),
        catalog=view(version=who.entitlement_version),
        scope=who,
    )


def raw_rows(db: duckdb.DuckDBPyConnection, compiled: CompiledQuery) -> QueryRows:
    sql = duckdb_sql(compiled)
    params = {
        p.name: list(p.value) if isinstance(p.value, tuple) else p.value
        for p in compiled.parameters
        if f"${p.name}" in sql
    }
    cursor = db.execute(sql, params)
    columns = tuple(d[0] for d in cursor.description)
    return QueryRows(columns, cursor.fetchall())


def released(
    db: duckdb.DuckDBPyConnection,
    executive: str,
    sql: str,
    who: ProductScope = SCOPE_A,
    values: Mapping[str, ScalarValue] | None = None,
) -> ReleasedResult:
    compiled = compile_for(executive, sql, who, values)
    return BOUNDARY.release(
        compiled, raw_rows(db, compiled), catalog=view(version=who.entitlement_version)
    )


def ref(executive: str, kind: str, raw: int) -> str:
    return KEYRING.for_executive(executive).reference(kind, raw)


def cells(result: ReleasedResult) -> list[Any]:
    return [cell for row in result.rows for cell in row]


def assert_no_pii(result: ReleasedResult, *, raw_ids: Sequence[int] = ()) -> None:
    text = repr(result.records())
    for needle in PII_STRINGS:
        assert needle not in text, needle
    # Integer and text cells only: a float total may equal a small number.
    values = [c for c in cells(result) if type(c) in (int, str)]
    for age in AGES.values():
        if age is not None:
            assert age not in values and str(age) not in values
    for raw in raw_ids:
        assert raw not in values and str(raw) not in values
    assert MASTER_KEY.decode() not in text
