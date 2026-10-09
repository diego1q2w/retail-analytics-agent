"""Truthful outcomes for local investigations whose process ended.

The local manager runs investigations as tasks of the API process. Unlike a
durable runtime it does not resume them: when the process stopped (shutdown)
or died, the work is interrupted. At startup - before admitting new work and
while holding the local-manager lock - ``InterruptedRunSweep`` finds the
orphaned local runs and ends each through ``InvestigationRuntime.interrupt``:
no model call, no tool or warehouse job run again, queued requests discarded
with a notice instead of started. History, evidence, reports, budgets and
operation/job records stay; the session accepts a new request.
"""

from __future__ import annotations

from retail_analytics.application.contracts.investigations import (
    InterruptionSweep,
)
from retail_analytics.application.investigation_runtime import InvestigationRuntime
from retail_analytics.application.ports.investigation_interruption import (
    OrphanedLocalRuns,
)
from retail_analytics.application.ports.investigations import InvestigationInputs
from retail_analytics.application.ports.persistence import SessionRepository
from retail_analytics.domain.conversation import MessageRole
from retail_analytics.domain.investigations import input_id_for, message_id_for

QUEUED_NOT_STARTED_NOTICE = (
    "The analysis service stopped before your queued request started, so it "
    "was not run. Send it again if you still need it."
)


class InterruptedRunSweep:
    def __init__(
        self,
        *,
        orphans: OrphanedLocalRuns,
        runtime: InvestigationRuntime,
        inputs: InvestigationInputs,
        sessions: SessionRepository,
    ) -> None:
        self._orphans = orphans
        self._runtime = runtime
        self._inputs = inputs
        self._sessions = sessions

    async def sweep(self, *, owner: str) -> InterruptionSweep:
        """End every orphaned local run (not owned by ``owner``)."""
        interrupted: list[str] = []
        for run_id in await self._orphans.active_runs(owner=owner):
            await self._runtime.interrupt(run_id)
            interrupted.append(run_id)
        discarded = 0
        for session_id in await self._orphans.queued_sessions():
            discarded += await self._discard_queued(session_id)
        return InterruptionSweep(tuple(interrupted), discarded)

    async def _discard_queued(self, session_id: str) -> int:
        # The run they waited behind already ended; tell the user in the
        # session instead of starting them silently.
        discarded = 0
        while (queued := await self._inputs.next_queued(session_id)) is not None:
            await self._inputs.discard(queued.input_id)
            await self._sessions.append_message(
                message_id=message_id_for(input_id_for(queued.input_id, "discarded")),
                session_id=session_id,
                role=MessageRole.ASSISTANT,
                content=QUEUED_NOT_STARTED_NOTICE,
            )
            discarded += 1
        return discarded
