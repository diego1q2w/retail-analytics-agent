"""Parsing of the backend's Server-Sent Events."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SseMessage:
    event: str
    data: str
    id: str | None = None


@dataclass(frozen=True, slots=True)
class SseComment:
    """A ``: keepalive`` line. It proves the connection is alive."""


@dataclass(frozen=True, slots=True)
class SseRetry:
    milliseconds: int


SseItem = SseMessage | SseComment | SseRetry


def parse_sse(lines: Iterable[str]) -> Iterator[SseItem]:
    event = "message"
    data: list[str] = []
    event_id: str | None = None
    for raw in lines:
        line = raw.rstrip("\r\n")
        if line == "":
            if data or event != "message" or event_id is not None:
                yield SseMessage(event, "\n".join(data), event_id)
            event, data, event_id = "message", [], None
            continue
        if line.startswith(":"):
            yield SseComment()
            continue
        name, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if name == "event":
            event = value
        elif name == "data":
            data.append(value)
        elif name == "id":
            event_id = value
        elif name == "retry" and value.isdigit():
            yield SseRetry(int(value))
