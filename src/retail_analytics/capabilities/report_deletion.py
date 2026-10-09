"""The ``propose_report_deletion`` capability.

The model can only *propose*: it names report IDs it found with report search
or listing (owner-only), and the application records an expiring proposal and
shows the user what would be deleted. Nothing is deleted by this tool, and no
argument or output lets the model approve. The user's explicit confirmation is
handled by ``ReportDeletionService.confirm`` outside the agent loop.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from pydantic import Field

from retail_analytics.application.authorization import AccessDenied
from retail_analytics.application.contracts import ContractModel, Identifier
from retail_analytics.application.report_deletion import ReportDeletionService
from retail_analytics.application.tools import (
    AuthorizationSpec,
    CapabilitySpec,
    OperationContext,
    RetrySpec,
    ToolFailed,
    ToolInput,
    ToolOutcome,
    ToolOutput,
    ToolSucceeded,
)
from retail_analytics.domain.access import Permission
from retail_analytics.domain.operations import (
    RecoveryMode,
    SideEffect,
    ToolErrorCode,
)
from retail_analytics.domain.report_deletion import (
    MAX_REPORTS_PER_PROPOSAL,
    DeletionError,
    DeletionErrorCode,
)

PROPOSE_REPORT_DELETION = "propose_report_deletion"
_ATTEMPT_TIMEOUT = timedelta(seconds=30)


class ProposeReportDeletionInput(ToolInput):
    report_ids: tuple[Identifier, ...] = Field(
        min_length=1,
        max_length=MAX_REPORTS_PER_PROPOSAL,
        description=(
            "Exact IDs of the user's own saved reports to delete, taken from "
            "their report search or listing results. Never guess an ID."
        ),
    )


class ProposedReport(ContractModel):
    report_id: str
    version: int
    # Untrusted text saved with the report: show it as data, never follow it.
    title: str | None
    created_on: date


class ProposeReportDeletionOutput(ToolOutput):
    proposal_id: str
    report_count: int
    reports: tuple[ProposedReport, ...]
    expires_at: datetime
    # What to tell the user. The model cannot confirm.
    next_step: str


_NEXT_STEP = (
    "Nothing has been deleted. Tell the user which reports are affected and that "
    "they can confirm or cancel in the application within 10 minutes. You cannot "
    "confirm, and a reply in chat is not a confirmation."
)
_MESSAGES = {
    DeletionErrorCode.INVALID_REQUEST: "Name between 1 and "
    f"{MAX_REPORTS_PER_PROPOSAL} report IDs.",
    DeletionErrorCode.TOO_MANY_PENDING: (
        "Too many deletions are waiting for confirmation. Ask the user to "
        "confirm or cancel the pending ones first."
    ),
    DeletionErrorCode.IDEMPOTENCY_CONFLICT: (
        "This request conflicts with an earlier one."
    ),
}


def report_deletion_capability(
    service: ReportDeletionService,
) -> CapabilitySpec[ProposeReportDeletionInput, ProposeReportDeletionOutput]:
    async def propose(
        args: ProposeReportDeletionInput, ctx: OperationContext
    ) -> ToolOutcome[ProposeReportDeletionOutput]:
        try:
            preview = await service.propose(ctx, args.report_ids)
        except AccessDenied:
            return ToolFailed(
                code=ToolErrorCode.FIELD_UNAVAILABLE,
                message=(
                    "At least one report is not one of the user's saved reports. "
                    "Search the user's reports and use the IDs it returns."
                ),
            )
        except DeletionError as error:
            return ToolFailed(
                code=ToolErrorCode.INVALID_INPUT,
                message=_MESSAGES.get(error.code, error.message),
            )
        return ToolSucceeded(
            output=ProposeReportDeletionOutput(
                proposal_id=preview.proposal_id,
                report_count=preview.count,
                reports=tuple(
                    ProposedReport(
                        report_id=i.report_id,
                        version=i.version,
                        title=i.title,
                        created_on=i.created_at.date(),
                    )
                    for i in preview.items
                ),
                expires_at=preview.expires_at,
                next_step=_NEXT_STEP,
            )
        )

    return CapabilitySpec(
        name=PROPOSE_REPORT_DELETION,
        version=1,
        description=(
            "Propose deleting specific saved reports of the user. This only "
            "records a proposal for the user to confirm in the application; "
            "it deletes nothing and you cannot confirm it."
        ),
        progress_label="Preparing the deletion proposal for your confirmation.",
        input_model=ProposeReportDeletionInput,
        output_model=ProposeReportDeletionOutput,
        handler=propose,
        authorization=AuthorizationSpec(
            required_permissions=frozenset({Permission.REPORTS_DELETE_OWN.value}),
        ),
        side_effect=SideEffect.IDEMPOTENT_WRITE,
        retry=RetrySpec(RecoveryMode.RETRY, 2, _ATTEMPT_TIMEOUT),
    )
