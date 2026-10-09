from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import datetime
from typing import Protocol

from retail_analytics.application.contracts.telemetry import Attributes, Label, Metric


class SpanHandle(Protocol):
    """A started span; setters are best effort and never raise."""

    def set(self, attributes: Attributes) -> None: ...

    def event(self, name: str, attributes: Attributes) -> None: ...

    def fail(self, error_type: str) -> None: ...


class TelemetrySink(Protocol):
    """Exporter port (tracing and metrics). Receives already sanitized data.

    Implementations are bounded and non-blocking: when the backend is slow or
    down they drop data instead of delaying the caller.
    """

    def span(
        self,
        name: str,
        *,
        run_id: str | None,
        attributes: Attributes,
        start: datetime | None = None,
        root: bool = False,
    ) -> AbstractContextManager[SpanHandle]: ...

    def count(self, metric: Metric, value: float, labels: dict[Label, str]) -> None: ...

    def observe(
        self, metric: Metric, value: float, labels: dict[Label, str]
    ) -> None: ...

    def flush(self, timeout_seconds: float) -> None: ...
