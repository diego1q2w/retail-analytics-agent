"""The principal a tool call acts for, read from the run's durable record."""

from __future__ import annotations

from retail_analytics.application.contracts.authorization import Principal
from retail_analytics.application.ports.investigations import RunPrincipals
from retail_analytics.application.tools import OperationContext


async def run_principal(
    principals: RunPrincipals, ctx: OperationContext
) -> Principal | None:
    """The run's recorded principal, if it is the executive of ``ctx``.

    Services that authorize by ``Principal`` re-resolve current authority from
    it; the trusted context only names which run and executive this is.
    """
    principal = await principals.get(ctx.execution.correlation.run_id)
    if principal is None or principal.executive_id != ctx.execution.executive_id:
        return None
    return principal
