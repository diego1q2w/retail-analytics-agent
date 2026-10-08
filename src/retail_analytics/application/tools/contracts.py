"""Versioned tool call and result contracts shared by every capability.

A capability's arguments subclass ``ToolInput`` and its success payload
subclasses ``ToolOutput``. Every outcome is one of four statuses:

- ``succeeded``: validated output (``empty`` marks a valid empty result).
- ``pending``: a known external operation is still running; check it later.
- ``unknown``: the outcome is uncertain; reconcile ``reference`` before any retry.
- ``failed``: a sanitized error with a ``ToolErrorCode``.

Pending and unknown are deliberately not failures: treating them as errors
would invite blind resubmission of work that may already exist.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, JsonValue, StringConstraints

from retail_analytics.application.contracts import (
    CapabilityName,
    ContractModel,
    Identifier,
    SchemaVersion,
)
from retail_analytics.domain.operations import ToolErrorCode

SafeMessage = Annotated[str, StringConstraints(min_length=1, max_length=500)]


class ToolInput(ContractModel):
    """Base for model-supplied arguments. Never holds identity or authority."""


class ToolOutput(ContractModel):
    """Base for a capability's validated success payload."""


class ToolCall(ContractModel):
    """A model's request to run a catalog tool, before validation."""

    schema_version: SchemaVersion = 1
    call_id: Identifier
    name: CapabilityName
    arguments: dict[str, JsonValue] = Field(default_factory=dict)


class InputIssue(ContractModel):
    """Where and how arguments failed validation. Never carries input values."""

    location: tuple[Annotated[str, StringConstraints(max_length=64)] | int, ...]
    issue: Annotated[str, StringConstraints(max_length=64)]


class ToolSucceeded[OutputT: ToolOutput](ContractModel):
    status: Literal["succeeded"] = "succeeded"
    output: OutputT
    empty: bool = False


class ToolPending(ContractModel):
    status: Literal["pending"] = "pending"
    reference: Identifier
    summary: SafeMessage | None = None


class ToolOutcomeUnknown(ContractModel):
    status: Literal["unknown"] = "unknown"
    reference: Identifier | None = None
    summary: SafeMessage | None = None


class ToolFailed(ContractModel):
    status: Literal["failed"] = "failed"
    code: ToolErrorCode
    message: SafeMessage
    issues: tuple[InputIssue, ...] = ()


type ToolOutcome[OutputT: ToolOutput] = (
    ToolSucceeded[OutputT] | ToolPending | ToolOutcomeUnknown | ToolFailed
)


class ToolResult[OutputT: ToolOutput](ContractModel):
    """What the loop receives back for one tool call."""

    schema_version: SchemaVersion = 1
    call_id: Identifier
    capability: CapabilityName
    # None when the name did not resolve to an authorized capability.
    capability_version: int | None = Field(ge=1)
    operation_id: Identifier
    outcome: Annotated[
        ToolSucceeded[OutputT] | ToolPending | ToolOutcomeUnknown | ToolFailed,
        Field(discriminator="status"),
    ]
