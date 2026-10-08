from __future__ import annotations

from typing import Protocol

from retail_analytics.application.contracts.progress import ProgressUpdate


class ProgressSink(Protocol):
    """Port to the run-event store/stream; implemented by adapters."""

    async def publish(self, update: ProgressUpdate) -> None: ...
