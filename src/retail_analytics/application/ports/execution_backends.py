from __future__ import annotations

from typing import Protocol

from retail_analytics.application.contracts.execution_backends import (
    ForeignExecutions,
)
from retail_analytics.domain.runs import ExecutionBackend


class ActiveExecutions(Protocol):
    """Unfinished work owned by an execution backend."""

    async def owned_by(
        self, backend: ExecutionBackend, *, sample: int
    ) -> ForeignExecutions:
        """Active runs of ``backend`` (count and up to ``sample`` run IDs,
        oldest first) and pending queued requests in sessions it leads."""
        ...
