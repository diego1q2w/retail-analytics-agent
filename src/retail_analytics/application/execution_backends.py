"""Run ownership across execution backends.

Every run records the backend that executes it (``runs.execution_backend``).
Neither backend takes over the other's work: a Temporal workflow cannot be
continued in-process, and a local run has no workflow history to replay. So a
process starting with one backend first checks that the other backend leaves
no active run or queued request behind, and refuses to start otherwise. The
operator then finishes or cancels that work with its original backend (or
stays on it). Nothing is converted, reassigned or reset here.
"""

from __future__ import annotations

from retail_analytics.application.contracts.execution_backends import (
    ForeignExecutions,
)
from retail_analytics.application.ports.execution_backends import ActiveExecutions
from retail_analytics.domain.runs import ExecutionBackend

SAMPLE_RUN_IDS = 5


class IncompatibleActiveExecutions(Exception):
    """The other backend still owns unfinished work. Carries no secrets."""

    def __init__(self, selected: ExecutionBackend, foreign: ForeignExecutions) -> None:
        self.selected = selected
        self.foreign = foreign
        super().__init__(
            f"{foreign.active_runs} active run(s) and {foreign.queued_requests} "
            f"queued request(s) belong to the {foreign.backend.value} backend"
        )


def other_backend(backend: ExecutionBackend) -> ExecutionBackend:
    return (
        ExecutionBackend.TEMPORAL
        if backend is ExecutionBackend.LOCAL
        else ExecutionBackend.LOCAL
    )


class ExecutionOwnership:
    def __init__(self, active: ActiveExecutions) -> None:
        self._active = active

    async def ensure_no_foreign_work(self, selected: ExecutionBackend) -> None:
        """Raise ``IncompatibleActiveExecutions`` when the other backend still
        owns an active run or a queued request."""
        foreign = await self._active.owned_by(
            other_backend(selected), sample=SAMPLE_RUN_IDS
        )
        if not foreign.empty:
            raise IncompatibleActiveExecutions(selected, foreign)
