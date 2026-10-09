from __future__ import annotations

from dataclasses import dataclass

from retail_analytics.domain.runs import ExecutionBackend


@dataclass(frozen=True)
class ForeignExecutions:
    """Unfinished work of one backend: what another backend must not take over."""

    backend: ExecutionBackend
    active_runs: int
    queued_requests: int
    sample_run_ids: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return self.active_runs == 0 and self.queued_requests == 0
