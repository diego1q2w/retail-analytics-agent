"""Recover persisted start/notification intents after a process interruption.

The dispatcher polls business intent, never the model or clarification status.
Temporal waits remain signal driven. Starting an already started workflow is
idempotent; pending inputs remain authoritative until the model consumes them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from retail_analytics.application.authorization import Principal
from retail_analytics.application.investigations import (
    InvestigationInputs,
    InvestigationLauncher,
    InvestigationScheduler,
)
from retail_analytics.domain.runs import RunStatus


@dataclass(frozen=True)
class RecoveryCandidate:
    run_id: str
    session_id: str
    principal: Principal
    request_text: str
    submission_key: str
    status: RunStatus


class RecoveryCandidates(Protocol):
    async def active(self) -> Sequence[RecoveryCandidate]: ...
    async def queued_sessions(self) -> Sequence[str]: ...


class InvestigationRecovery:
    def __init__(
        self,
        candidates: RecoveryCandidates,
        launcher: InvestigationLauncher,
        inputs: InvestigationInputs,
        scheduler: InvestigationScheduler,
    ) -> None:
        self._candidates = candidates
        self._launcher = launcher
        self._inputs = inputs
        self._scheduler = scheduler

    async def dispatch(self) -> None:
        for candidate in await self._candidates.active():
            await self._launcher.launch(
                candidate.principal,
                session_id=candidate.session_id,
                text=candidate.request_text,
                submission_key=candidate.submission_key,
            )
            if candidate.status is RunStatus.CANCELLING:
                await self._scheduler.request_cancel(candidate.run_id)
            elif await self._inputs.pending(candidate.run_id):
                await self._scheduler.notify_input(candidate.run_id)
        for session_id in await self._candidates.queued_sessions():
            await self._launcher.promote_next(session_id)
