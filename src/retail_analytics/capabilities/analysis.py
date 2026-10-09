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
from retail_analytics.application.contracts.query_compiler import AnalysisQuery
from retail_analytics.application.evidence import (
    EvidenceRejected,
    EvidenceService,
    QueryBasis,
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
# What the restricted compiler accepts; stated up front so the model does not
# spend correction attempts discovering it.
SQL_DIALECT_NOTE = (
    "Allowed: WITH (CTEs), JOIN on the declared joins, WHERE, GROUP BY, HAVING, "
    "ORDER BY, LIMIT, DISTINCT, scalar subqueries, CASE/IF, + - *, SUM, AVG, "
    "MIN, MAX, COUNT, COUNTIF, COALESCE, NULLIF, ABS, ROUND, LOWER, UPPER, "
    "DATE_TRUNC, EXTRACT, DATE_ADD, DATE_SUB, DATE_DIFF, CAST, IN, BETWEEN, "
    "LIKE, EXISTS. Not allowed: the / operator (use SAFE_DIVIDE(a, b)), window "
    "functions (OVER; use ORDER BY ... LIMIT in a CTE or a scalar subquery), "
    "SELECT * (COUNT(*) is fine). Give tables aliases and qualify columns "
    "(s.product_id) when joining. Pass literal values as named @parameters "
    "or DATE 'YYYY-MM-DD' literals."
)
_CANCELLED = "The query was cancelled and produced no result."
_RECONCILING = "The query is being stopped; its outcome is being confirmed."


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
) -> CapabilitySpec[ExecuteAnalysisInput, ExecuteAnalysisOutput]:
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
                operation = await operations.get(ctx.operation_id)
                if (
                    operation is None
                    or operation.status is not ToolExecutionStatus.SUCCEEDED
                ):
                    return ToolFailed(
                        code=ToolErrorCode.INTERNAL_ERROR,
                        message="The query result could not be verified.",
                    )
                effective = await preferences.effective(
                    principal, session_id=ctx.execution.correlation.session_id
                )
                try:
                    recorded = await evidence.record_query(
                        ctx,
                        outcome.compiled,
                        outcome.result,
                        QueryBasis(
                            definitions=frozenset(),
                            preference_fingerprint=effective.analytical_fingerprint,
                        ),
                        # Evidence identity includes its timestamp. Replaying a
                        # committed job must reproduce the same immutable record.
                        computed_at=operation.updated_at,
                    )
                except EvidenceRejected as rejected:
                    code = (
                        ToolErrorCode.ACCESS_DENIED
                        if rejected.reason
                        in ("stale_authorization", "no_product_scope")
                        else ToolErrorCode.INTERNAL_ERROR
                    )
                    return ToolFailed(code=code, message=rejected.message)
                recovery = classify(outcome)
                rows = len(outcome.result.rows)
                return ToolSucceeded(
                    output=ExecuteAnalysisOutput(
                        evidence_id=recorded.evidence_id,
                        columns=tuple(c.name for c in outcome.result.columns),
                        row_count=rows,
                        complete=recovery.complete,
                        truncated=recovery.truncated,
                        note=_note(rows, recovery.complete),
                    ),
                    empty=rows == 0,
                )
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
        progress_label="Running a query on the permitted data.",
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
    "SQL_DIALECT_NOTE",
    "ExecuteAnalysisInput",
    "ExecuteAnalysisOutput",
    "analysis_capability",
]
