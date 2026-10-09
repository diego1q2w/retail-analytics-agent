"""Loaders of the on-demand tool groups (``application.tool_focus``).

Each loader is an argument-free tool the model calls when the investigation
turns out to need a group that is not exposed yet. It changes nothing but the
run's recorded operations: the next model step reads the successful loader
operation and exposes the group's tools that the executive is authorized for
at that moment. A loader is visible only to executives who may use at least
one tool of its group, and loading never grants anything: every tool call is
authorized again when it runs.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from retail_analytics.application.investigation_policy import (
    LOAD_CURRENCY_TOOLS,
    LOAD_DELETION_TOOLS,
    LOAD_PREFERENCE_TOOLS,
    LOAD_REPORT_TOOLS,
)
from retail_analytics.application.tools import (
    AuthorizationSpec,
    CapabilitySpec,
    OperationContext,
    RetrySpec,
    ToolInput,
    ToolOutcome,
    ToolOutput,
    ToolSucceeded,
)
from retail_analytics.domain.access import Permission
from retail_analytics.domain.operations import RecoveryMode, SideEffect


class LoadToolsInput(ToolInput):
    """No arguments: the loader's name says which group."""


class LoadToolsOutput(ToolOutput):
    message: str


_READY = "Loaded. The tools you may use appear at your next step."


async def _load(args: LoadToolsInput, ctx: OperationContext) -> ToolOutcome[Any]:
    return ToolSucceeded(output=LoadToolsOutput(message=_READY))


def _loader(
    name: str, what: str, authorization: AuthorizationSpec
) -> CapabilitySpec[Any, Any]:
    return CapabilitySpec(
        name=name,
        version=1,
        description=(
            f"Make the tools to {what} available from your next step. Call it "
            "only when the request needs them."
        ),
        progress_label="Preparing the tools this request needs.",
        input_model=LoadToolsInput,
        output_model=LoadToolsOutput,
        handler=_load,
        authorization=authorization,
        side_effect=SideEffect.READ_ONLY,
        retry=RetrySpec(RecoveryMode.RETRY, 2, timedelta(seconds=10)),
    )


def tool_loader_capabilities() -> tuple[CapabilitySpec[Any, Any], ...]:
    """One loader per on-demand group, visible only to who may use its tools."""
    analysis = frozenset({Permission.ANALYSIS_READ.value})
    return (
        _loader(
            LOAD_REPORT_TOOLS,
            "save, read, list, search or export saved reports",
            # Saving needs both; a run needs analysis anyway.
            AuthorizationSpec(
                required_permissions=analysis | {Permission.REPORTS_READ_OWN.value}
            ),
        ),
        _loader(
            LOAD_DELETION_TOOLS,
            "find saved reports and propose deleting them",
            AuthorizationSpec(
                required_permissions=frozenset({Permission.REPORTS_DELETE_OWN.value})
            ),
        ),
        _loader(
            LOAD_PREFERENCE_TOOLS,
            "inspect, remember, forget, confirm or decline saved preferences",
            AuthorizationSpec(required_permissions=analysis),
        ),
        _loader(
            LOAD_CURRENCY_TOOLS,
            "convert amounts of recorded evidence to another currency",
            AuthorizationSpec(
                required_permissions=analysis, requires_product_scope=True
            ),
        ),
    )
