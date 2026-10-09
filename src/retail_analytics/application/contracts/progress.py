from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import (
    Annotated,
    Self,
)

from pydantic import (
    AwareDatetime,
    Field,
    StringConstraints,
    model_validator,
)

from retail_analytics.application.contracts import (
    CapabilityName,
    ContractModel,
    Correlation,
    Identifier,
    SchemaVersion,
)
from retail_analytics.domain.operations import ToolErrorCode

Summary = Annotated[str, StringConstraints(min_length=1, max_length=280)]


class EventKind(StrEnum):
    RUN_STARTED = "run.started"
    ANALYSIS_PROGRESS = "analysis.progress"
    TOOL_STARTED = "tool.started"
    TOOL_RETRYING = "tool.retrying"
    TOOL_PENDING = "tool.pending"
    TOOL_OUTCOME_UNKNOWN = "tool.outcome_unknown"
    TOOL_SUCCEEDED = "tool.succeeded"
    TOOL_FAILED = "tool.failed"
    INPUT_REQUIRED = "input.required"
    DELETION_PROPOSED = "deletion.proposed"
    RUN_COMPLETED = "run.completed"
    RUN_PARTIAL = "run.partial"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"

    @property
    def is_tool_event(self) -> bool:
        return self.value.startswith("tool.")


class EventSource(StrEnum):
    # Authoritative execution facts emitted by application code.
    APPLICATION = "application"
    # A brief grounded summary supplied by the model (analysis.progress only).
    MODEL = "model"


class ToolActivity(ContractModel):
    capability: CapabilityName
    # None when the name did not resolve to an authorized capability.
    capability_version: int | None = Field(ge=1)
    attempt: int = Field(default=1, ge=1)
    error_code: ToolErrorCode | None = None


class InputRequest(ContractModel):
    question_id: Identifier
    question: Annotated[str, StringConstraints(min_length=1, max_length=1000)]


class ProgressUpdate(ContractModel):
    schema_version: SchemaVersion = 1
    correlation: Correlation
    kind: EventKind
    source: EventSource = EventSource.APPLICATION
    summary: Summary
    tool: ToolActivity | None = None
    input_request: InputRequest | None = None
    # Only on deletion.proposed: the ID of a pending proposal, never its content.
    deletion_proposal_id: Identifier | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.kind.is_tool_event:
            if self.tool is None or self.correlation.operation_id is None:
                raise ValueError("tool events need tool activity and operation_id")
        elif self.tool is not None:
            raise ValueError("tool activity only belongs to tool events")
        if (self.tool is not None and self.tool.error_code is not None) != (
            self.kind is EventKind.TOOL_FAILED
        ):
            raise ValueError("error_code is required on, and only on, tool.failed")
        if (self.input_request is not None) != (self.kind is EventKind.INPUT_REQUIRED):
            raise ValueError(
                "input_request is required on, and only on, input.required"
            )
        if (self.deletion_proposal_id is not None) != (
            self.kind is EventKind.DELETION_PROPOSED
        ):
            raise ValueError(
                "deletion_proposal_id is required on, and only on, deletion.proposed"
            )
        if self.source is EventSource.MODEL and self.kind is not (
            EventKind.ANALYSIS_PROGRESS
        ):
            raise ValueError("the model may only supply analysis.progress summaries")
        return self


class ProgressEvent(ProgressUpdate):
    """A persisted, ordered event as delivered to clients."""

    event_id: Identifier
    sequence: int = Field(ge=1)
    occurred_at: AwareDatetime

    @classmethod
    def stamp(
        cls,
        update: ProgressUpdate,
        *,
        event_id: str,
        sequence: int,
        occurred_at: datetime,
    ) -> ProgressEvent:
        return cls.model_validate(
            {
                **update.model_dump(),
                "event_id": event_id,
                "sequence": sequence,
                "occurred_at": occurred_at,
            }
        )
