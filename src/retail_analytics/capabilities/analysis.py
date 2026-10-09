"""The ``execute_analysis`` capability: one guarded warehouse query.

The model supplies logical SQL over the permitted catalog, named analysis
values and a short purpose; identity, product scope and budgets come from the
trusted context. Each attempt goes through ``QueryExecutionService`` (fresh
authority, recompilation, reconcile-before-submit, deadline, result privacy
boundary) and a successful result is recorded as immutable evidence.

The model-facing output is deliberately compact: the evidence ID, column
names, row count and completeness. Released rows reach the model only through
context assembly (``ContextBuilder``), which re-checks authority on every
model request, so result rows never travel through workflow history.

Each record carries its definition basis, determined by trusted code. It is
analytical context, not proof that the SQL implemented a metric: the catalog
definitions whose fields the compiled query read, what terms such as
"revenue" meant under the effective preferences, the compiler's date window,
the date field and the time zone (UTC); for the latest-month query shape the
year comes from the released rows' own dates (no extra query). Saved reports
compare it with the reader's current definitions when displayed. A query
comparing several periods records no single period, so answers and reports
must say which periods were compared.

The handler owns its operation record (the warehouse job reference must be
recorded before submission), and charges a reformulation to the run budget
when this query follows a failed, correctable one.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta
from typing import Annotated

from pydantic import Field, StringConstraints

from retail_analytics.application.budgets import RunBudgets, budget_message
from retail_analytics.application.contracts.authorization import Principal
from retail_analytics.application.contracts.query_compiler import (
    AnalysisQuery,
    CompiledQuery,
)
from retail_analytics.application.contracts.sql_dialect import SQL_DIALECT_NOTE
from retail_analytics.application.contracts.telemetry import Span
from retail_analytics.application.evidence import (
    EvidenceRejected,
    EvidenceService,
    query_basis,
)
from retail_analytics.application.ports.investigations import RunPrincipals
from retail_analytics.application.ports.persistence import ToolExecutionRepository
from retail_analytics.application.preferences import PreferenceService
from retail_analytics.application.query_execution import (
    QUERY_CAPABILITY,
    QUERY_CAPABILITY_VERSION,
    QueryAttempt,
    QueryCancelled,
    QueryExecutionService,
    QueryFailed,
    QueryOutcomeUnknown,
    QueryPending,
    QuerySucceeded,
)
from retail_analytics.application.recovery import classify
from retail_analytics.application.scope_values import ScopeValueCheck
from retail_analytics.application.telemetry import telemetry
from retail_analytics.application.tools import (
    AuthorizationSpec,
    CapabilitySpec,
    OperationContext,
    RetrySpec,
    ToolFailed,
    ToolInput,
    ToolOutcome,
    ToolOutcomeUnknown,
    ToolOutput,
    ToolPending,
    ToolSucceeded,
)
from retail_analytics.domain.access import Permission
from retail_analytics.domain.budgets import BudgetExhausted
from retail_analytics.domain.executions import ToolExecution, ToolExecutionStatus
from retail_analytics.domain.metrics import MetricCatalog, default_catalog
from retail_analytics.domain.operations import RecoveryMode, SideEffect, ToolErrorCode

ParameterName = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
]
type AnalysisValue = str | int | float | bool | date

_CORRECTABLE = frozenset(
    {
        ToolErrorCode.INVALID_QUERY,
        ToolErrorCode.UNSUPPORTED_SQL,
        ToolErrorCode.INVALID_INPUT,
        ToolErrorCode.FIELD_UNAVAILABLE,
        ToolErrorCode.BUDGET_EXCEEDED,
    }
)
_ACCESS = "This data is not available to you."
_CANCELLED = "The query was cancelled and produced no result."
_RECONCILING = "The query is being stopped; its outcome is being confirmed."
_NOT_SAVED = (
    "The query finished, but its result could not be saved as evidence. A "
    "retry reads the finished query's result again; the query is not re-run."
)


class ExecuteAnalysisInput(ToolInput):
    sql: str = Field(
        min_length=1,
        max_length=20_000,
        description=(
            "One SELECT over the permitted relations (see list_relations and "
            "describe_relation). " + SQL_DIALECT_NOTE
        ),
    )
    parameters: dict[ParameterName, AnalysisValue] = Field(
        default_factory=dict,
        max_length=50,
        description="Values for the @parameters used in the SQL.",
    )
    purpose: str = Field(
        min_length=1,
        max_length=200,
        description="One short sentence: what this query checks.",
    )


class ExecuteAnalysisOutput(ToolOutput):
    evidence_id: str
    columns: tuple[str, ...]
    row_count: int
    # Every row of the answer is included (nothing withheld or cut).
    complete: bool
    truncated: bool
    note: str


def analysis_capability(
    *,
    executions: QueryExecutionService,
    evidence: EvidenceService,
    principals: RunPrincipals,
    preferences: PreferenceService,
    budgets: RunBudgets,
    operations: ToolExecutionRepository,
    attempt_timeout: timedelta = timedelta(minutes=4),
    metrics: MetricCatalog | None = None,
    scope_values: ScopeValueCheck | None = None,
) -> CapabilitySpec[ExecuteAnalysisInput, ExecuteAnalysisOutput]:
    catalog = metrics or default_catalog()

    async def record(
        outcome: QuerySucceeded, ctx: OperationContext, principal: Principal
    ) -> ToolOutcome[ExecuteAnalysisOutput]:
        """Record the released rows as evidence for this operation (idempotent:
        one record per operation, identical when replayed)."""
        operation = await operations.get(ctx.operation_id)
        if operation is None or operation.status is not ToolExecutionStatus.SUCCEEDED:
            return ToolFailed(
                code=ToolErrorCode.INTERNAL_ERROR,
                message="The query result could not be verified.",
            )
        effective = await preferences.effective(
            principal, session_id=ctx.execution.correlation.session_id
        )
        recorded = await evidence.record_query(
            ctx,
            outcome.compiled,
            outcome.result,
            # Definitions, term meanings, period and date basis come from the
            # compiled query, the metric catalog and the effective preferences,
            # never from the model.
            query_basis(
                outcome.compiled,
                metrics=catalog,
                effective=effective,
                released=outcome.result,
            ),
            # Evidence identity includes its timestamp. Replaying a committed
            # job must reproduce the same immutable record.
            computed_at=operation.updated_at,
        )
        recovery = classify(outcome)
        rows = len(outcome.result.rows)
        note = _note(rows, recovery.complete)
        if scope_values is not None and outcome.compiled.value_filters:
            outside = await scope_values.assess(
                outcome.compiled.value_filters, ctx.execution.product_scope
            )
            if outside is not None:
                note = f"{outside.note()} {note}"
        return ToolSucceeded(
            output=ExecuteAnalysisOutput(
                evidence_id=recorded.evidence_id,
                columns=tuple(c.name for c in outcome.result.columns),
                row_count=rows,
                complete=recovery.complete,
                truncated=recovery.truncated,
                note=note,
            ),
            empty=rows == 0,
        )

    async def record_result(
        outcome: QuerySucceeded, ctx: OperationContext, principal: Principal
    ) -> ToolOutcome[ExecuteAnalysisOutput]:
        """``record`` under its own span. The warehouse job already finished:
        a failure here never re-runs it. A temporary failure is retried by the
        tool runner, whose next attempt re-reads the finished job's rows by
        its recorded ID (``QuerySucceeded.replayed``) and records them."""
        run_id = ctx.execution.correlation.run_id
        with telemetry().span(
            Span.EVIDENCE,
            run_id=run_id,
            attributes={
                "run_id": run_id,
                "operation_id": ctx.operation_id,
                "attempt": ctx.attempt,
                "job_id": outcome.job.job_id,
                "replayed": outcome.replayed,
            },
        ) as span:
            try:
                result = await record(outcome, ctx, principal)
            except EvidenceRejected as rejected:
                result = _rejected(rejected)
                span.set({"reason": rejected.reason})
            except Exception as error:
                # Never echo the exception: it may carry data values.
                result = ToolFailed(
                    code=ToolErrorCode.TEMPORARY_FAILURE, message=_NOT_SAVED
                )
                span.set({"reason": "store_failed", "error_type": type(error).__name__})
            match result:
                case ToolSucceeded():
                    evidence_id = result.output.evidence_id
                    kind = "recovered" if outcome.replayed else "recorded"
                    span.set({"outcome": kind, "evidence_id": evidence_id})
                    if span.captures:
                        span.outputs(
                            {"job_id": outcome.job.job_id, "evidence_id": evidence_id}
                        )
                case ToolFailed():
                    code = result.code.value.lower()
                    span.set({"outcome": "failed", "error_code": code})
                    span.fail(code)
        return result

    async def execute_analysis(
        args: ExecuteAnalysisInput, ctx: OperationContext
    ) -> ToolOutcome[ExecuteAnalysisOutput]:
        run_id = ctx.execution.correlation.run_id
        principal = await principals.get(run_id)
        if principal is None or principal.executive_id != ctx.execution.executive_id:
            return ToolFailed(code=ToolErrorCode.ACCESS_DENIED, message=_ACCESS)
        refused = await _charge_correction(budgets, operations, ctx)
        if refused is not None:
            return refused
        outcome = await executions.execute(
            QueryAttempt(
                principal=principal,
                run_id=run_id,
                operation_id=ctx.operation_id,
                attempt=ctx.attempt,
                query=AnalysisQuery(args.sql, dict(args.parameters)),
                trace_id=ctx.execution.correlation.trace_id,
            )
        )
        match outcome:
            case QuerySucceeded():
                return await record_result(outcome, ctx, principal)
            case QueryPending():
                return ToolPending(reference=outcome.job.job_id)
            case QueryOutcomeUnknown():
                reference = None if outcome.job is None else outcome.job.job_id
                return ToolOutcomeUnknown(reference=reference)
            case QueryFailed() if outcome.stopping is not None:
                return ToolOutcomeUnknown(
                    reference=outcome.stopping.job_id, summary=_RECONCILING
                )
            case QueryFailed():
                return ToolFailed(code=outcome.code, message=outcome.message)
            case QueryCancelled():
                return ToolFailed(code=ToolErrorCode.INTERNAL_ERROR, message=_CANCELLED)

    async def describe_query(
        args: ExecuteAnalysisInput, ctx: OperationContext
    ) -> str | None:
        run_id = ctx.execution.correlation.run_id
        principal = await principals.get(run_id)
        if principal is None or principal.executive_id != ctx.execution.executive_id:
            return None
        compiled = await executions.describe(
            QueryAttempt(
                principal=principal,
                run_id=run_id,
                operation_id=ctx.operation_id,
                attempt=ctx.attempt,
                query=AnalysisQuery(args.sql, dict(args.parameters)),
                trace_id=ctx.execution.correlation.trace_id,
            )
        )
        return None if compiled is None else query_progress_label(compiled)

    return CapabilitySpec(
        name=QUERY_CAPABILITY,
        version=QUERY_CAPABILITY_VERSION,
        description=(
            "Run one analytical SELECT over the permitted relations and record "
            "the result as evidence. Returns the evidence id, columns, row count "
            "and whether the result is complete; the rows appear in your "
            "evidence context. Aggregate in SQL rather than fetching detail rows. "
            "Cite evidence ids for every figure you report."
        ),
        progress_label=QUERY_LABEL,
        progress_context=describe_query,
        input_model=ExecuteAnalysisInput,
        output_model=ExecuteAnalysisOutput,
        handler=execute_analysis,
        authorization=AuthorizationSpec(
            required_permissions=frozenset({Permission.ANALYSIS_READ.value}),
            requires_product_scope=True,
        ),
        side_effect=SideEffect.EXTERNAL_JOB,
        retry=RetrySpec(RecoveryMode.RECONCILE_FIRST, 3, attempt_timeout),
    )


QUERY_LABEL = "Running a query."

# Progress wording for a compiled query: fixed application templates chosen
# from the logical fields the compiler verified. Never the model's purpose,
# SQL, column aliases or filter values (product, brand or customer names), so
# the label says what kind of figure is computed and nothing about the data.
_MEASURES: tuple[tuple[str, str], ...] = (
    ("sale_amount", "revenue"),
    ("order_ref", "order counts"),
    ("customer_ref", "customer counts"),
)
_BREAKDOWNS: tuple[tuple[str, str, str], ...] = (
    ("products", "category", "by category"),
    ("products", "brand", "by brand"),
    ("products", "department", "by department"),
    ("customers", "country", "by country"),
    ("customers", "state", "by state"),
    ("products", "product_id", "by product"),
    ("sales_items", "product_id", "by product"),
)


def query_progress_label(compiled: CompiledQuery) -> str | None:
    """``Comparing revenue by category.``, ``Calculating order counts.`` or
    None (the generic label) when the query's shape is not recognized."""
    aggregated = {
        ref.field
        for output in compiled.outputs
        if not output.direct
        for ref in output.sources
    }
    measure = next((noun for f, noun in _MEASURES if f in aggregated), None)
    if measure is None:
        return None
    grouped = {
        (ref.relation, ref.field)
        for output in compiled.outputs
        if output.direct
        for ref in output.sources
    }
    breakdown = next(
        (words for relation, f, words in _BREAKDOWNS if (relation, f) in grouped),
        None,
    )
    if breakdown is not None:
        return f"Comparing {measure} {breakdown}."
    if compiled.latest_month is not None:
        return f"Calculating {measure} for the latest month."
    return f"Calculating {measure}."


def _rejected(rejected: EvidenceRejected) -> ToolFailed:
    """A specific class for each reason evidence cannot be recorded."""
    match rejected.reason:
        case "stale_authorization" | "no_product_scope":
            code = ToolErrorCode.ACCESS_DENIED
        case "too_large":
            # Reformulating can resolve it: fewer or more aggregated rows.
            code = ToolErrorCode.INVALID_QUERY
        case "unstorable_value":
            # Our defect, not the query's: never tell the model its valid SQL
            # was wrong. Not retried (the same rows would fail the same way).
            code = ToolErrorCode.INTERNAL_ERROR
        case _:
            # Catalog moved between compile and release: a retry recompiles
            # under the current catalog and re-reads the finished job.
            code = ToolErrorCode.TEMPORARY_FAILURE
    return ToolFailed(code=code, message=rejected.message)


def _note(rows: int, complete: bool) -> str:
    if rows == 0:
        return "Valid empty result: check filters and assumptions before concluding."
    if not complete:
        return (
            "Incomplete result (rows were cut or withheld): do not compute totals "
            "from it; aggregate at the source or narrow the query."
        )
    return "Complete result; rows are in your evidence context."


async def _charge_correction(
    budgets: RunBudgets,
    operations: ToolExecutionRepository,
    ctx: OperationContext,
) -> ToolFailed | None:
    """A query right after a failed, correctable one is its reformulation."""
    run_id = ctx.execution.correlation.run_id
    if await operations.get(ctx.operation_id) is not None:
        return None  # already charged (or not a correction) when first recorded
    previous = _latest_query(await operations.for_run(run_id), ctx.operation_id)
    if (
        previous is None
        or previous.status is not ToolExecutionStatus.FAILED
        or previous.error_code not in _CORRECTABLE
    ):
        return None
    try:
        await budgets.reserve_correction(
            run_id, ctx.operation_id, corrects=previous.operation_id
        )
    except BudgetExhausted as error:
        return ToolFailed(
            code=ToolErrorCode.BUDGET_EXCEEDED, message=budget_message(error.resource)
        )
    return None


def _latest_query(
    executions: Sequence[ToolExecution], excluding: str
) -> ToolExecution | None:
    candidates = [
        e
        for e in executions
        if e.capability == QUERY_CAPABILITY and e.operation_id != excluding
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda e: (e.created_at, e.operation_id))


__all__ = [
    "QUERY_LABEL",
    "SQL_DIALECT_NOTE",
    "ExecuteAnalysisInput",
    "ExecuteAnalysisOutput",
    "analysis_capability",
    "query_progress_label",
]
