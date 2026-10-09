"""Builders for evidence tests: real compiled queries and released results.

Results come from the privacy suite's DuckDB oracle with the production keyed
derivations, so compiled queries carry real trusted and secret parameters
that evidence must never keep.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from retail_analytics.application.contracts import Correlation
from retail_analytics.application.contracts.query_compiler import CompiledQuery
from retail_analytics.application.contracts.tools import (
    ExecutionContext,
    OperationContext,
)
from retail_analytics.application.evidence import EvidenceService, QueryBasis
from retail_analytics.application.result_privacy import (
    ReleasedResult,
    ResultLimits,
    ResultPrivacyBoundary,
)
from retail_analytics.domain.access import Permission, ProductScope
from retail_analytics.domain.evidence import DefinitionRef, Requirements, ReusePolicy
from retail_analytics.domain.periods import DateWindow
from tests.unit.evidence.fakes import Clock, FakeEvidenceStore, Ids
from tests.unit.privacy.support import (
    BOUNDARY,
    EXEC_A,
    EXEC_B,
    SCOPE_A,
    SCOPE_B,
    compile_for,
    customer_database,
    raw_rows,
)
from tests.unit.sql_compiler.support import view

SPEND_SQL = (
    "SELECT customer_ref, SUM(sale_amount) AS spend FROM sales_items "
    "WHERE sale_amount >= @min_amount GROUP BY customer_ref"
)
REVENUE = DefinitionRef("completed_item_sales", 1)
SEPTEMBER = DateWindow(date(2026, 9, 1), date(2026, 10, 1))
FINGERPRINT = "fp-default"
PERMISSIONS = frozenset({Permission.ANALYSIS_READ.value})

__all__ = ["EXEC_A", "EXEC_B", "SCOPE_A", "SCOPE_B"]


def compiled_and_released(
    executive: str = EXEC_A,
    scope: ProductScope = SCOPE_A,
    sql: str = SPEND_SQL,
    values: dict[str, Any] | None = None,
    *,
    max_rows: int | None = None,
) -> tuple[CompiledQuery, ReleasedResult]:
    values = {"min_amount": 1} if values is None else values
    compiled = compile_for(executive, sql, scope, values)
    boundary = (
        BOUNDARY
        if max_rows is None
        else ResultPrivacyBoundary(ResultLimits(max_rows=max_rows))
    )
    result = boundary.release(
        compiled,
        raw_rows(customer_database(), compiled),
        catalog=view(version=scope.entitlement_version),
    )
    return compiled, result


def basis(**overrides: Any) -> QueryBasis:
    values: dict[str, Any] = {
        "definitions": frozenset({REVENUE}),
        "preference_fingerprint": FINGERPRINT,
        "grain": ("customer_ref",),
        "period": SEPTEMBER,
        "analytical_slots": frozenset({"metric_definition:revenue", "time_zone"}),
    }
    values.update(overrides)
    return QueryBasis(**values)


def requirements(compiled: CompiledQuery, **overrides: Any) -> Requirements:
    values: dict[str, Any] = {
        "catalog_version": compiled.catalog_version,
        "policy_version": 1,
        "preference_fingerprint": FINGERPRINT,
        "definitions": frozenset({REVENUE}),
        "period": SEPTEMBER,
    }
    values.update(overrides)
    return Requirements(**values)


def context(
    executive: str = EXEC_A,
    scope: ProductScope = SCOPE_A,
    *,
    session: str = "ses-a",
    run: str = "run-1",
    permissions: frozenset[str] = PERMISSIONS,
) -> ExecutionContext:
    return ExecutionContext(
        executive, permissions, scope, Correlation(session_id=session, run_id=run)
    )


def operation(ctx: ExecutionContext, operation_id: str) -> OperationContext:
    return OperationContext(ctx, operation_id)


@dataclass
class Env:
    clock: Clock = field(default_factory=Clock)
    store: FakeEvidenceStore = field(init=False)
    service: EvidenceService = field(init=False)

    def __post_init__(self) -> None:
        self.store = FakeEvidenceStore(clock=self.clock)
        self.service = EvidenceService(
            self.store,
            self.store,
            clock=self.clock,
            new_id=Ids(),
            policy=ReusePolicy(),
            imports=self.store,
            scopes=self.store,
        )
