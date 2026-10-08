from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from retail_analytics.application.contracts.investigations import RecoveryCandidate


class RecoveryCandidates(Protocol):
    async def active(self) -> Sequence[RecoveryCandidate]: ...
    async def queued_sessions(self) -> Sequence[str]: ...
