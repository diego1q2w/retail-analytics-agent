"""Queries about brands outside the permitted scope are refused before any job;
a permitted brand without sales still runs (a genuine zero)."""

from __future__ import annotations

from collections.abc import Iterable, Iterator

import pytest
from duckdb import DuckDBPyConnection as Connection

from retail_analytics.application.contracts.query_compiler import AnalysisQuery
from retail_analytics.application.query_execution import (
    SCOPE_REFUSAL,
    QueryAttempt,
    QueryExecutionService,
    QueryFailed,
    QuerySucceeded,
    is_compiler_rejection,
)
from retail_analytics.application.result_privacy import ResultPrivacyBoundary
from retail_analytics.application.scope_values import ScopeValueCheck
from retail_analytics.domain.executions import ToolExecutionStatus
from retail_analytics.domain.operations import ToolErrorCode
from tests.unit.privacy.support import COMPILERS, EXEC_A, SCOPE_A, customer_database
from tests.unit.query_execution.fakes import (
    FakeAuthority,
    FakeWarehouse,
    MemoryOperations,
    MemoryQueryJobs,
    oracle_runner,
)
from tests.unit.query_execution.test_service import PRINCIPAL, SETTINGS

# The synced snapshot: products 1 and 3 (SCOPE_A) are Alpha, 2 is Beta.
SNAPSHOT = {"1": "Alpha", "2": "Beta", "3": "Alpha"}
BRAND_REVENUE = (
    "SELECT SUM(s.sale_amount) AS revenue, COUNT(*) AS items "
    "FROM sales_items AS s JOIN products AS p ON s.product_id = p.product_id "
    "WHERE s.item_status = 'Complete' AND {where}"
)


class SnapshotBrands:
    def __init__(self, snapshot: dict[str, str]) -> None:
        self.snapshot = snapshot

    async def brands_within(self, product_ids: Iterable[str]) -> frozenset[str]:
        return frozenset(self.snapshot[p] for p in product_ids if p in self.snapshot)


class Setup:
    def __init__(self, db: Connection, snapshot: dict[str, str]) -> None:
        self.operations = MemoryOperations()
        self.jobs = MemoryQueryJobs(self.operations)
        self.warehouse = FakeWarehouse(oracle_runner(db))
        self.service = QueryExecutionService(
            settings=SETTINGS,
            authority=FakeAuthority(EXEC_A, SCOPE_A),
            compilers=COMPILERS,
            boundary=ResultPrivacyBoundary(),
            warehouse=self.warehouse,
            operations=self.operations,
            jobs=self.jobs,
            scope_values=ScopeValueCheck(SnapshotBrands(snapshot)),
        )

    async def run(self, where: str, op: str = "op-1") -> object:
        return await self.service.execute(
            QueryAttempt(
                PRINCIPAL,
                "run-1",
                op,
                1,
                AnalysisQuery(BRAND_REVENUE.format(where=where)),
            )
        )


@pytest.fixture
def db() -> Iterator[Connection]:
    connection = customer_database()
    yield connection
    connection.close()


@pytest.mark.asyncio
async def test_brand_outside_scope_is_refused_without_a_job(db: Connection) -> None:
    setup = Setup(db, SNAPSHOT)
    outcome = await setup.run("p.brand = 'Beta'")

    assert isinstance(outcome, QueryFailed)
    assert outcome.code is ToolErrorCode.ACCESS_DENIED
    assert outcome.rejected
    assert "Outside the user's permitted scope" in outcome.message
    assert "'Beta'" in outcome.message
    assert "never 0" in outcome.message
    assert "without jokes" in outcome.message
    assert setup.warehouse.created == []
    record = setup.operations.records["op-1"]
    assert record.status is ToolExecutionStatus.FAILED
    assert record.error_detail == SCOPE_REFUSAL
    assert is_compiler_rejection(record)


@pytest.mark.asyncio
async def test_unknown_name_gets_the_same_wording(db: Connection) -> None:
    """A name that exists nowhere reads exactly like another manager's brand."""
    setup = Setup(db, SNAPSHOT)
    known = await setup.run("p.brand = 'Beta'", op="op-1")
    unknown = await setup.run("p.brand = 'Gamma'", op="op-2")

    assert isinstance(known, QueryFailed) and isinstance(unknown, QueryFailed)
    assert known.message.replace("Beta", "X") == unknown.message.replace("Gamma", "X")


@pytest.mark.asyncio
async def test_misspelled_permitted_brand_suggests_only_permitted_names(
    db: Connection,
) -> None:
    setup = Setup(db, SNAPSHOT)
    outcome = await setup.run("p.brand = 'Alpah'")

    assert isinstance(outcome, QueryFailed)
    assert "'Alpha'" in outcome.message
    assert "Beta" not in outcome.message


@pytest.mark.asyncio
async def test_permitted_brand_with_no_sales_runs_and_may_be_zero(
    db: Connection,
) -> None:
    setup = Setup(db, SNAPSHOT)
    outcome = await setup.run(
        "LOWER(p.brand) = 'alpha' AND s.ordered_date >= DATE '2030-01-01'"
    )

    assert isinstance(outcome, QuerySucceeded)
    assert outcome.result.rows[0][1] == 0
    assert len(setup.warehouse.created) == 1


@pytest.mark.asyncio
async def test_mixed_brands_run(db: Connection) -> None:
    setup = Setup(db, SNAPSHOT)
    outcome = await setup.run("p.brand IN ('Alpha', 'Beta')")

    assert isinstance(outcome, QuerySucceeded)


@pytest.mark.asyncio
async def test_without_a_snapshot_nothing_is_judged(db: Connection) -> None:
    setup = Setup(db, {})
    outcome = await setup.run("p.brand = 'Beta'")

    assert isinstance(outcome, QuerySucceeded)
