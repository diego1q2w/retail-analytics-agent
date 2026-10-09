"""A recording telemetry sink for tests (no exporter, no network)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime

from retail_analytics.application.contracts.telemetry import (
    Attributes,
    CapturedPayload,
    Label,
    Metric,
)
from retail_analytics.application.ports.telemetry import SpanHandle
from retail_analytics.application.telemetry import Telemetry


@dataclass
class RecordedSpan:
    name: str
    run_id: str | None
    attributes: Attributes
    root: bool = False
    events: list[tuple[str, Attributes]] = field(default_factory=list)
    failed: str | None = None
    ended: bool = False
    payloads: list[CapturedPayload] = field(default_factory=list)

    def content(self, side: str) -> object:
        """The last captured content of ``side`` ("inputs"/"outputs")."""
        found = [p.content for p in self.payloads if p.side.value == side]
        return found[-1] if found else None


class _Handle:
    def __init__(self, span: RecordedSpan) -> None:
        self._span = span

    def set(self, attributes: Attributes) -> None:
        self._span.attributes.update(attributes)

    def event(self, name: str, attributes: Attributes) -> None:
        self._span.events.append((name, attributes))

    def fail(self, error_type: str) -> None:
        self._span.failed = error_type

    def payload(self, payload: CapturedPayload) -> None:
        self._span.payloads.append(payload)


class RecordingSink:
    def __init__(self) -> None:
        self.spans: list[RecordedSpan] = []
        self.counts: list[tuple[Metric, float, dict[Label, str]]] = []
        self.observations: list[tuple[Metric, float, dict[Label, str]]] = []

    @contextmanager
    def span(
        self,
        name: str,
        *,
        run_id: str | None,
        attributes: Attributes,
        start: datetime | None = None,
        root: bool = False,
    ) -> Iterator[SpanHandle]:
        recorded = RecordedSpan(name, run_id, dict(attributes), root)
        self.spans.append(recorded)
        try:
            yield _Handle(recorded)
        finally:
            recorded.ended = True

    def count(self, metric: Metric, value: float, labels: dict[Label, str]) -> None:
        self.counts.append((metric, value, dict(labels)))

    def observe(self, metric: Metric, value: float, labels: dict[Label, str]) -> None:
        self.observations.append((metric, value, dict(labels)))

    def flush(self, timeout_seconds: float) -> None:
        return None

    # -- queries ----------------------------------------------------------

    def named(self, name: str) -> list[RecordedSpan]:
        return [s for s in self.spans if s.name == name]

    def total(self, metric: Metric, **labels: str) -> float:
        wanted = {Label(k): v for k, v in labels.items()}
        return sum(
            value
            for m, value, got in self.counts
            if m is metric and all(got.get(k) == v for k, v in wanted.items())
        )

    def everything(self) -> str:
        """All recorded data as one string, for canary scans."""
        return self._dump(content=True)

    def metadata(self) -> str:
        """Everything except captured content (attributes, events, metrics)."""
        return self._dump(content=False)

    def _dump(self, *, content: bool) -> str:
        return json.dumps(
            {
                "spans": [
                    [
                        s.name,
                        s.run_id,
                        s.attributes,
                        s.events,
                        s.failed,
                        [p.content for p in s.payloads] if content else [],
                    ]
                    for s in self.spans
                ],
                "counts": [
                    [m.value, v, {k.value: x for k, x in lab.items()}]
                    for m, v, lab in self.counts
                ],
                "obs": [
                    [m.value, v, {k.value: x for k, x in lab.items()}]
                    for m, v, lab in self.observations
                ],
            },
            default=str,
        )


def recording(*, capture_content: bool = True) -> tuple[Telemetry, RecordingSink]:
    sink = RecordingSink()
    return Telemetry(sink, capture_content=capture_content), sink
