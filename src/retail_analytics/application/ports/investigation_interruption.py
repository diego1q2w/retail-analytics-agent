from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol


class OrphanedLocalRuns(Protocol):
    """Runs of the local in-process backend whose execution is gone.

    Only runs owned by the local backend are ever listed: Temporal-owned runs
    and their queued requests are never touched by the local manager.
    """

    async def active_runs(self, *, owner: str) -> Sequence[str]:
        """Active local runs not owned by the manager instance ``owner``."""
        ...

    async def queued_sessions(self) -> Sequence[str]:
        """Sessions led by a local run, with no active run but pending queued
        requests (their run ended before the next request was started)."""
        ...
