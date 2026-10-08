"""Fake SQL, chart and delivery capabilities registered like real ones.

They exercise the contracts only: no warehouse, rendering, mail or web calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from retail_analytics.application.contracts import Correlation
from retail_analytics.application.contracts.progress import ProgressUpdate
from retail_analytics.application.tools import (
    AuthorizationSpec,
    CapabilitySpec,
    ExecutionContext,
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
from retail_analytics.domain.access import ProductScope
from retail_analytics.domain.operations import (
    RecoveryMode,
    SideEffect,
    ToolErrorCode,
)

SQL = "execute_analysis"
CHART = "render_chart"
DELIVER = "deliver_report"


class SqlInput(ToolInput):
    sql: Annotated[str, StringConstraints(min_length=1, max_length=10_000)]
    parameters: dict[str, str | int | float] = Field(default_factory=dict)
    purpose: Annotated[str, StringConstraints(min_length=1, max_length=200)]


class SqlOutput(ToolOutput):
    columns: tuple[str, ...]
    rows: tuple[tuple[str | int | float | None, ...], ...]
    truncated: bool
    evidence_id: str


class ChartInput(ToolInput):
    evidence_id: str
    kind: Literal["bar", "line"]


class ChartOutput(ToolOutput):
    artifact_id: str
    evidence_id: str


class DeliveryInput(ToolInput):
    report_id: str
    report_version: int = Field(ge=1)
    recipient_ref: str


class DeliveryOutput(ToolOutput):
    delivery_id: str


@dataclass
class Calls:
    """Records what handlers received, to prove rejected calls never ran."""

    seen: list[tuple[str, ToolInput, OperationContext]] = field(default_factory=list)


async def _sql(
    calls: Calls, args: SqlInput, ctx: OperationContext
) -> ToolOutcome[SqlOutput]:
    calls.seen.append(("sql", args, ctx))
    if args.sql == "pending":
        return ToolPending(reference=f"job-{ctx.operation_id}")
    if args.sql == "unknown":
        return ToolOutcomeUnknown(reference=f"job-{ctx.operation_id}")
    if args.sql == "invalid":
        return ToolFailed(code=ToolErrorCode.INVALID_QUERY, message="Bad query.")
    if args.sql == "raise":
        raise RuntimeError("leaky detail: customer jane@example.com")
    rows: tuple[tuple[str | int | float | None, ...], ...] = (
        () if args.sql == "empty" else (("A", 30),)
    )
    return ToolSucceeded(
        output=SqlOutput(
            columns=("product", "revenue"),
            rows=rows,
            truncated=False,
            evidence_id=f"ev-{ctx.operation_id}",
        ),
        empty=not rows,
    )


def sql_spec(calls: Calls) -> CapabilitySpec[SqlInput, SqlOutput]:
    async def handler(args: SqlInput, ctx: OperationContext) -> ToolOutcome[SqlOutput]:
        return await _sql(calls, args, ctx)

    return CapabilitySpec(
        name=SQL,
        version=1,
        description="Run one guarded analytical query.",
        progress_label="Running an analysis query.",
        input_model=SqlInput,
        output_model=SqlOutput,
        handler=handler,
        authorization=AuthorizationSpec(
            required_permissions=frozenset({"analyze"}), requires_product_scope=True
        ),
        side_effect=SideEffect.EXTERNAL_JOB,
        retry=RetrySpec(RecoveryMode.RECONCILE_FIRST, 3, timedelta(minutes=2)),
    )


def chart_spec(calls: Calls) -> CapabilitySpec[ChartInput, ChartOutput]:
    async def handler(
        args: ChartInput, ctx: OperationContext
    ) -> ToolOutcome[ChartOutput]:
        calls.seen.append(("chart", args, ctx))
        if args.evidence_id == "wrong-output":
            return ToolSucceeded(
                output=SqlOutput(  # type: ignore[arg-type]
                    columns=(), rows=(), truncated=False, evidence_id="x"
                )
            )
        return ToolSucceeded(
            output=ChartOutput(artifact_id="art-1", evidence_id=args.evidence_id)
        )

    return CapabilitySpec(
        name=CHART,
        version=2,
        description="Render a chart artifact from existing evidence.",
        progress_label="Drawing a chart.",
        input_model=ChartInput,
        output_model=ChartOutput,
        handler=handler,
        authorization=AuthorizationSpec(required_permissions=frozenset({"analyze"})),
        side_effect=SideEffect.READ_ONLY,
        retry=RetrySpec(RecoveryMode.RETRY, 3, timedelta(seconds=30)),
    )


def delivery_spec(calls: Calls) -> CapabilitySpec[DeliveryInput, DeliveryOutput]:
    async def handler(
        args: DeliveryInput, ctx: OperationContext
    ) -> ToolOutcome[DeliveryOutput]:
        calls.seen.append(("deliver", args, ctx))
        if args.recipient_ref == "raise":
            raise TimeoutError("smtp timeout")
        return ToolSucceeded(output=DeliveryOutput(delivery_id=f"d-{ctx.operation_id}"))

    return CapabilitySpec(
        name=DELIVER,
        version=1,
        description="Send a saved report version to an authorized recipient.",
        progress_label="Delivering the report.",
        input_model=DeliveryInput,
        output_model=DeliveryOutput,
        handler=handler,
        authorization=AuthorizationSpec(required_permissions=frozenset({"deliver"})),
        side_effect=SideEffect.EXTERNAL_DELIVERY,
        retry=RetrySpec(RecoveryMode.RECONCILE_FIRST, 2, timedelta(seconds=30)),
    )


def execution_context(
    *,
    permissions: frozenset[str] = frozenset({"analyze", "deliver"}),
    products: frozenset[str] = frozenset({"p-1"}),
) -> ExecutionContext:
    return ExecutionContext(
        executive_id="exec-1",
        permissions=permissions,
        product_scope=ProductScope(product_ids=products, entitlement_version=3),
        correlation=Correlation(session_id="s-1", run_id="r-1", trace_id="t-1"),
    )


@dataclass
class RecordingSink:
    updates: list[ProgressUpdate] = field(default_factory=list)

    async def publish(self, update: ProgressUpdate) -> None:
        self.updates.append(update)
