"""Typed tool contracts, trusted execution context, registry and gateway."""

from retail_analytics.application.contracts.tools import (
    ExecutionContext,
    OperationContext,
)
from retail_analytics.application.tools.contracts import (
    InputIssue,
    ToolCall,
    ToolFailed,
    ToolInput,
    ToolOutcome,
    ToolOutcomeUnknown,
    ToolOutput,
    ToolPending,
    ToolResult,
    ToolSucceeded,
)
from retail_analytics.application.tools.gateway import invoke
from retail_analytics.application.tools.registry import (
    AuthorizationSpec,
    CapabilityRegistry,
    CapabilitySpec,
    Handler,
    RegistrationError,
    RetrySpec,
    ToolDescriptor,
)

__all__ = [
    "AuthorizationSpec",
    "CapabilityRegistry",
    "CapabilitySpec",
    "ExecutionContext",
    "Handler",
    "InputIssue",
    "OperationContext",
    "RegistrationError",
    "RetrySpec",
    "ToolCall",
    "ToolDescriptor",
    "ToolFailed",
    "ToolInput",
    "ToolOutcome",
    "ToolOutcomeUnknown",
    "ToolOutput",
    "ToolPending",
    "ToolResult",
    "ToolSucceeded",
    "invoke",
]
