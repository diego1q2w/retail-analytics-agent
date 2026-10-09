"""In-process telemetry sink that keeps finished spans for an evaluation run.

The real-model evaluation installs it (``application.telemetry.use_telemetry``)
to learn which provider actually answered each run: the ``model.attempt`` spans
(provider, model, outcome, fallback origin) and the run's root span
(``answered_by``). It receives only what the telemetry facade already
sanitized; metrics are ignored. Bounded: older spans are dropped past
``max_spans``.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

from retail_analytics.application.contracts.evaluation import RecordedSpan
from retail_analytics.application.contracts.telemetry import (
    Attributes,
    Label,
    Metric,
)


class _Handle:
    def __init__(self, attributes: Attributes) -> None:
        self.attributes = dict(attributes)

    def set(self, attributes: Attributes) -> None:
        self.attributes.update(attributes)

    def event(self, name: str, attributes: Attributes) -> None:
        return None

    def fail(self, error_type: str) -> None:
        self.attributes["error_type"] = error_type


class RecordingTelemetrySink:
    """``TelemetrySink`` that records finished spans (thread-safe)."""

    def __init__(self, max_spans: int = 100_000) -> None:
        self._spans: deque[RecordedSpan] = deque(maxlen=max_spans)
        self._lock = threading.Lock()

    @contextmanager
    def span(
        self,
        name: str,
        *,
        run_id: str | None,
        attributes: Attributes,
        start: datetime | None = None,
        root: bool = False,
    ) -> Iterator[_Handle]:
        handle = _Handle(attributes)
        try:
            yield handle
        finally:
            with self._lock:
                self._spans.append(RecordedSpan(name, run_id, handle.attributes))

    def count(self, metric: Metric, value: float, labels: dict[Label, str]) -> None:
        return None

    def observe(self, metric: Metric, value: float, labels: dict[Label, str]) -> None:
        return None

    def flush(self, timeout_seconds: float) -> None:
        return None

    def spans(self) -> tuple[RecordedSpan, ...]:
        with self._lock:
            return tuple(self._spans)
