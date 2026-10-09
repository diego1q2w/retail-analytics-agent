"""A finished warehouse query whose evidence could not be saved (T12-F1).

Runs ``execute_analysis`` through the real tool runner, query execution
service, evidence service and result privacy boundary, with faults injected
into evidence persistence after the warehouse job succeeded. The runner is
the tool path of both runtimes: a Temporal activity retry calls ``run`` again
with the same tool-call ID, which the replay cases below reproduce.

Recovery must read the finished job again by its recorded ID: one submitted
job, one admission (the run's query count), one settled charge and one
evidence record, however often persistence fails or the call is replayed.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast

import pytest
from duckdb import DuckDBPyConnection as Connection

from retail_analytics.application.budgets import RetryDecision
from retail_analytics.application.contracts import Correlation
from retail_analytics.application.contracts.authorization import Principal
from retail_analytics.application.contracts.evidence import NewEvidence
from retail_analytics.application.contracts.query_compiler import CompiledQuery
from retail_analytics.application.contracts.telemetry import Label, Metric, Span
from retail_analytics.application.contracts.tools import ExecutionContext
from retail_analytics.application.contracts.warehouse_jobs import JobStatistics
from retail_analytics.application.evidence import EvidenceRejected, EvidenceService
from retail_analytics.application.query_execution import (
    QueryExecutionService,
    QueryExecutionSettings,
)
from retail_analytics.application.result_privacy import ResultPrivacyBoundary
from retail_analytics.application.telemetry import Telemetry, use_telemetry
from retail_analytics.application.tool_runner import ToolRunner, ToolRunnerSettings
from retail_analytics.application.tools import (
    CapabilityRegistry,
    ToolFailed,
    ToolResult,
    ToolSucceeded,
)
from retail_analytics.capabilities.analysis import _rejected, analysis_capability
from retail_analytics.domain import evidence as evidence_domain
from retail_analytics.domain.budgets import BudgetResource
from retail_analytics.domain.evidence import Evidence, ReusePolicy
from retail_analytics.domain.executions import ToolExecutionStatus
from retail_analytics.domain.investigations import operation_id_for
from retail_analytics.domain.operations import ToolErrorCode
from retail_analytics.domain.preferences import EffectivePreferences
from retail_analytics.domain.runs import RunStatus
from tests.unit.evidence.fakes import Clock, FakeEvidenceStore, Ids
from tests.unit.privacy.support import COMPILERS, EXEC_A, SCOPE_A, customer_database
from tests.unit.query_execution.fakes import (
    FakeAuthority,
    FakeWarehouse,
    MemoryOperations,
    MemoryQueryJobs,
    oracle_runner,
)
from tests.unit.telemetry.recording import RecordedSpan, RecordingSink
from tests.unit.tools.fakes import RecordingSink as ProgressSink

pytestmark = pytest.mark.asyncio

RUN = "run-1"
CALL = "call-1"
PRINCIPAL = Principal(EXEC_A, frozenset({"analysis:read"}))
TOTAL = {"sql": "SELECT SUM(sale_amount) AS sales FROM sales_items", "purpose": "x"}
# DuckDB (the oracle, like the evaluation's fixture warehouse) returns a naive
# TIMESTAMP for DATE_TRUNC over a date: rows evidence cannot store.
MONTHLY = {
    "sql": (
        "SELECT DATE_TRUNC(ordered_date, MONTH) AS month_start, "
        "SUM(sale_amount) AS sales FROM sales_items GROUP BY month_start"
    ),
    "purpose": "x",
}


class StoreDown(Exception):
    """The evidence database failed (connection lost, timeout, ...)."""


@dataclass
class FlakyStore(FakeEvidenceStore):
    """Fails the next ``fail_before`` writes before committing, then the next
    ``fail_after`` writes after committing (a lost acknowledgement)."""

    fail_before: int = 0
    fail_after: int = 0
    writes: int = 0

    async def record(self, new: NewEvidence) -> Evidence:
        self.writes += 1
        if self.fail_before:
            self.fail_before -= 1
            raise StoreDown
        stored = await super().record(new)
        if self.fail_after:
            self.fail_after -= 1
            raise StoreDown
        return stored


@dataclass
class Budgets:
    """The run budget seams the runner and query execution use."""

    admitted: list[tuple[str, int]] = field(default_factory=list)
    settled: list[tuple[str, int, JobStatistics]] = field(default_factory=list)
    retries: int = 0
    # Set once the run's active time is spent (the 120 s deadline).
    spent: bool = False

    async def admit(
        self,
        context: ExecutionContext,
        operation_id: str,
        submission: int,
        compiled: CompiledQuery,
        estimated_bytes: int,
    ) -> None:
        if (operation_id, submission) not in self.admitted:
            self.admitted.append((operation_id, submission))

    async def settle(
        self,
        run_id: str,
        operation_id: str,
        submission: int,
        statistics: JobStatistics,
    ) -> None:
        self.settled.append((operation_id, submission, statistics))

    async def with_budget(self, context: ExecutionContext) -> ExecutionContext:
        return context

    async def snapshot(self, run_id: str) -> Any:
        if not self.spent:
            return None
        return SimpleNamespace(
            paused=False,
            remaining=lambda: {BudgetResource.ACTIVE_TIME: 0.0},
            exhausted=lambda: {BudgetResource.ACTIVE_TIME},
        )

    async def retry_decision(self, run_id: str, failures: int) -> RetryDecision:
        self.retries += 1
        return RetryDecision(failures < 3)

    async def reserve_correction(
        self, run_id: str, operation_id: str, *, corrects: str
    ) -> None:
        return None


class World:
    def __init__(self, db: Connection) -> None:
        self.operations = MemoryOperations()
        self.jobs = MemoryQueryJobs(self.operations)
        self.warehouse = FakeWarehouse(oracle_runner(db))
        self.budgets = Budgets()
        self.fetches = 0
        self.max_iterations = 200
        fetch = self.warehouse.fetch_rows

        async def counted(ref: Any, *, max_rows: int) -> Any:
            self.fetches += 1
            return await fetch(ref, max_rows=max_rows)

        self.warehouse.fetch_rows = counted  # type: ignore[method-assign]
        self.store = FlakyStore(clock=Clock())
        self.telemetry = RecordingSink()

    def runner(self) -> ToolRunner:
        """A fresh runner and services: what a retried activity builds."""
        queries = QueryExecutionService(
            settings=QueryExecutionSettings(
                project="test-project", location="US", wait_seconds=0.05
            ),
            authority=FakeAuthority(EXEC_A, SCOPE_A),
            compilers=COMPILERS,
            boundary=ResultPrivacyBoundary(),
            warehouse=self.warehouse,
            operations=self.operations,
            jobs=self.jobs,
            admission=self.budgets,
            usage=self.budgets,
        )
        evidence = EvidenceService(
            self.store,
            self.store,
            clock=self.store.clock,
            new_id=Ids(),
            policy=ReusePolicy(),
            imports=self.store,
            scopes=self.store,
        )
        principals = SimpleNamespace(get=_principal)
        capability = analysis_capability(
            executions=queries,
            evidence=evidence,
            principals=cast(Any, principals),
            preferences=cast(Any, SimpleNamespace(effective=_preferences)),
            budgets=cast(Any, self.budgets),
            operations=self.operations,
        )
        return ToolRunner(
            registry=CapabilityRegistry([capability]),
            resolver=cast(Any, SimpleNamespace(context_for_run=_context)),
            principals=cast(Any, principals),
            runs=cast(Any, SimpleNamespace(get_run=_running)),
            operations=self.operations,
            budgets=cast(Any, self.budgets),
            progress=ProgressSink(),
            settings=ToolRunnerSettings(
                follow_seconds=0, max_iterations=self.max_iterations
            ),
            sleep=_no_wait,
        )

    async def call(self, args: dict[str, Any], call_id: str = CALL) -> ToolResult[Any]:
        with use_telemetry(Telemetry(self.telemetry)):
            return await self.runner().run(RUN, call_id, "execute_analysis", args)

    def spans(self, name: Span) -> list[RecordedSpan]:
        return [s for s in self.telemetry.spans if s.name == name.value]

    def billed_bytes_counted(self) -> float:
        total = 0.0
        for metric, value, labels in self.telemetry.counts:
            if metric is Metric.QUERY_BYTES and labels.get(Label.KIND) == "billed":
                total += value
        return total


async def _principal(run_id: str) -> Principal:
    return PRINCIPAL


async def _preferences(principal: Principal, *, session_id: str) -> Any:
    return EffectivePreferences(())


async def _context(
    principal: Principal, run_id: str, *, trace_id: str | None = None
) -> ExecutionContext:
    return ExecutionContext(
        EXEC_A,
        frozenset({"analysis:read"}),
        SCOPE_A,
        Correlation(session_id="ses-1", run_id=run_id),
    )


async def _running(run_id: str) -> Any:
    return SimpleNamespace(status=RunStatus.RUNNING)


async def _no_wait(seconds: float) -> None:
    return None


@pytest.fixture
def db() -> Iterator[Connection]:
    connection = customer_database()
    yield connection
    connection.close()


@pytest.fixture
def world(db: Connection) -> World:
    return World(db)


def succeeded(result: ToolResult[Any]) -> Any:
    assert isinstance(result.outcome, ToolSucceeded), result.outcome
    return result.outcome.output


def charged_once(world: World) -> None:
    """One job submitted, admitted (counted) and settled; one evidence."""
    assert len(world.warehouse.created) == 1
    assert world.warehouse.submit_calls == 1
    assert len(world.budgets.admitted) == 1
    assert len({(op, n) for op, n, _ in world.budgets.settled}) == 1
    assert len(world.budgets.settled) == 1
    assert len(world.store.records) == 1


async def test_store_failure_after_the_job_recovers_from_the_finished_job(
    world: World,
) -> None:
    world.store.fail_before = 1

    output = succeeded(await world.call(TOTAL))

    charged_once(world)
    assert output.evidence_id in world.store.records
    queries = world.spans(Span.QUERY)
    assert [q.attributes["replayed"] for q in queries] == [False, True]
    job_id = world.warehouse.created[0]
    assert {q.attributes["job_id"] for q in queries} == {job_id}
    # The failure and the recovery are both in the trace, with the job ID.
    first, second = world.spans(Span.EVIDENCE)
    assert first.attributes["outcome"] == "failed"
    assert first.attributes["error_code"] == "temporary_failure"
    assert first.attributes["reason"] == "store_failed"
    assert first.failed == "temporary_failure"
    assert second.attributes["outcome"] == "recovered"
    assert second.attributes["evidence_id"] == output.evidence_id
    assert first.attributes["job_id"] == second.attributes["job_id"] == job_id
    # Bytes are counted once although the result was read twice.
    assert world.billed_bytes_counted() == 10 * 1024 * 1024


async def test_lost_acknowledgement_keeps_one_evidence_record(world: World) -> None:
    world.store.fail_after = 1

    output = succeeded(await world.call(TOTAL))

    charged_once(world)
    assert world.store.writes == 2
    assert list(world.store.records) == [output.evidence_id]


async def test_replayed_call_returns_the_same_evidence_without_a_new_job(
    world: World,
) -> None:
    world.store.fail_before = 1
    first = succeeded(await world.call(TOTAL))

    # A Temporal activity retry (or a worker restart) repeats the call.
    again = succeeded(await world.call(TOTAL))

    assert again.evidence_id == first.evidence_id
    charged_once(world)
    assert world.billed_bytes_counted() == 10 * 1024 * 1024


async def test_persistent_store_failure_is_reported_truthfully_without_rerunning(
    world: World,
) -> None:
    world.store.fail_before = 10

    result = await world.call(TOTAL)

    assert isinstance(result.outcome, ToolFailed)
    assert result.outcome.code is ToolErrorCode.TEMPORARY_FAILURE
    assert "not re-run" in result.outcome.message
    assert len(world.warehouse.created) == 1
    assert len(world.budgets.admitted) == 1
    assert len(world.budgets.settled) == 1
    assert not world.store.records
    assert all(s.attributes["outcome"] == "failed" for s in world.spans(Span.EVIDENCE))

    # Once the store is back, the same call recovers from the same job.
    world.store.fail_before = 0
    output = succeeded(await world.call(TOTAL))
    charged_once(world)
    assert world.spans(Span.EVIDENCE)[-1].attributes["outcome"] == "recovered"
    assert output.evidence_id in world.store.records


async def test_unstorable_value_is_an_application_error_not_a_query_error(
    world: World,
) -> None:
    # The SQL is valid; our code cannot store the value it legitimately
    # produced (the T26-F9 evaluation failure). Not the model's fault.
    result = await world.call(MONTHLY)

    assert isinstance(result.outcome, ToolFailed)
    assert result.outcome.code is ToolErrorCode.INTERNAL_ERROR
    assert "application problem, not a problem with the query" in (
        result.outcome.message
    )
    # Deterministic: not retried, so the warehouse ran it exactly once.
    assert len(world.warehouse.created) == 1
    assert world.budgets.retries == 0
    assert not world.store.records
    (span,) = world.spans(Span.EVIDENCE)
    assert span.attributes["reason"] == "unstorable_value"
    assert span.attributes["error_code"] == "internal_error"
    assert span.failed == "internal_error"
    assert span.attributes["job_id"] == world.warehouse.created[0]
    # Visible in metrics as an internal error of the tool.
    assert any(
        metric is Metric.TOOL_CALLS and labels.get(Label.ERROR_CODE) == "internal_error"
        for metric, _, labels in world.telemetry.counts
    )


async def test_too_large_result_asks_for_a_query_that_can_be_recorded(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Reformulating (aggregating, fewer rows) can resolve this one.
    monkeypatch.setattr(evidence_domain, "MAX_PAYLOAD_BYTES", 8)

    result = await world.call(TOTAL)

    assert isinstance(result.outcome, ToolFailed)
    assert result.outcome.code is ToolErrorCode.INVALID_QUERY
    assert "aggregate further or return fewer rows" in result.outcome.message
    assert len(world.warehouse.created) == 1
    assert world.budgets.retries == 0
    (span,) = world.spans(Span.EVIDENCE)
    assert span.attributes["reason"] == "too_large"


@pytest.mark.parametrize(
    ("reason", "code"),
    [
        ("stale_authorization", ToolErrorCode.ACCESS_DENIED),
        ("no_product_scope", ToolErrorCode.ACCESS_DENIED),
        ("too_large", ToolErrorCode.INVALID_QUERY),
        ("unstorable_value", ToolErrorCode.INTERNAL_ERROR),
        ("invalid", ToolErrorCode.TEMPORARY_FAILURE),
    ],
)
async def test_each_rejection_reason_has_its_own_class(
    reason: str, code: ToolErrorCode
) -> None:
    failed = _rejected(EvidenceRejected(reason))
    assert failed.code is code
    assert failed.message == EvidenceRejected(reason).message


async def test_no_recovery_or_re_read_after_the_active_deadline(world: World) -> None:
    # The store fails; before the retry the run's active time runs out.
    world.store.fail_before = 10
    original = world.budgets.retry_decision

    async def deadline_passes(run_id: str, failures: int) -> RetryDecision:
        world.budgets.spent = True
        return await original(run_id, failures)

    world.budgets.retry_decision = deadline_passes  # type: ignore[method-assign]

    result = await world.call(TOTAL)

    assert isinstance(result.outcome, ToolFailed)
    assert result.outcome.code is ToolErrorCode.BUDGET_EXCEEDED
    assert world.fetches == 1  # the finished job was not read again
    assert len(world.warehouse.created) == 1
    assert [s.attributes["outcome"] for s in world.spans(Span.EVIDENCE)] == ["failed"]
    assert not world.store.records

    # A later replay (activity retry) after the deadline does not recover.
    world.store.fail_before = 0
    again = await world.call(TOTAL)
    assert isinstance(again.outcome, ToolFailed)
    assert again.outcome.code is ToolErrorCode.BUDGET_EXCEEDED
    assert world.fetches == 1
    assert not world.store.records
    assert len(world.spans(Span.EVIDENCE)) == 1


async def test_a_cancelled_job_is_never_recovered(world: World) -> None:
    # The deadline stop path cancelled the job by its recorded ID.
    world.warehouse.polls_to_finish = 10**6
    world.max_iterations = 1
    pending = await world.call(TOTAL)
    assert not isinstance(pending.outcome, ToolSucceeded | ToolFailed)
    operation_id = operation_id_for(RUN, CALL)
    service = QueryExecutionService(
        settings=QueryExecutionSettings(project="test-project", location="US"),
        authority=FakeAuthority(EXEC_A, SCOPE_A),
        compilers=COMPILERS,
        boundary=ResultPrivacyBoundary(),
        warehouse=world.warehouse,
        operations=world.operations,
        jobs=world.jobs,
    )
    await service.cancel(RUN, operation_id)
    assert world.operations.records[operation_id].status is (
        ToolExecutionStatus.CANCELLED
    )

    world.max_iterations = 200
    result = await world.call(TOTAL)

    assert not isinstance(result.outcome, ToolSucceeded)
    assert world.fetches == 0
    assert not world.store.records
    assert not world.spans(Span.EVIDENCE)
    assert world.warehouse.submit_calls == 1
