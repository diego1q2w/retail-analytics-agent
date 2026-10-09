"""Durable run progress over Server-Sent Events.

Each SSE message carries one persisted ``ProgressEvent`` as JSON with
``id: <event_id>`` and ``event: <kind>``. A client that reconnects with
``Last-Event-ID`` receives exactly the later events, in sequence order, with
no gaps and no repeats; nothing is started or restarted by connecting.

Each poll re-authorizes the caller and releases events through the output
gate under the authority current at that moment. Comment lines keep idle
connections alive. The stream ends with an ``end`` message (no ID) after the
run's terminal event; a revoked caller gets an ``error`` message and the
stream closes. Connections are also closed after a maximum duration; clients
resume from their last event ID.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable

from retail_analytics.application.contracts.authorization import Principal
from retail_analytics.application.contracts.conversations import EventBatch
from retail_analytics.application.contracts.progress import EventKind, ProgressEvent
from retail_analytics.interfaces.http.errors import to_api_error
from retail_analytics.interfaces.http.services import Conversations, StreamSettings

TERMINAL_EVENTS = frozenset(
    {
        EventKind.RUN_COMPLETED,
        EventKind.RUN_PARTIAL,
        EventKind.RUN_FAILED,
        EventKind.RUN_CANCELLED,
    }
)


def format_event(event: ProgressEvent) -> bytes:
    data = event.model_dump_json()
    return f"id: {event.event_id}\nevent: {event.kind.value}\ndata: {data}\n\n".encode()


def format_message(name: str, payload: dict[str, object]) -> bytes:
    return f"event: {name}\ndata: {json.dumps(payload)}\n\n".encode()


async def event_stream(
    *,
    conversations: Conversations,
    principal: Principal,
    run_id: str,
    first: EventBatch,
    after_event_id: str | None,
    settings: StreamSettings,
    disconnected: Callable[[], Awaitable[bool]],
    clock: Callable[[], float] = time.monotonic,
) -> AsyncIterator[bytes]:
    """Yield SSE bytes starting with the already-authorized ``first`` batch."""
    started = clock()
    last_sent = started
    terminal_since: float | None = None
    cursor = after_event_id
    sequence: int | None = None
    batch = first
    yield f"retry: {settings.retry_millis}\n\n".encode()
    while True:
        for event in batch.events:
            yield format_event(event)
            cursor, sequence = event.event_id, event.sequence
            last_sent = clock()
            if event.kind in TERMINAL_EVENTS:
                yield format_message(
                    "end", {"run_id": run_id, "status": batch.status.value}
                )
                return
        now = clock()
        if batch.status.is_terminal and batch.caught_up:
            # The status changes before the terminal event is appended.
            terminal_since = now if terminal_since is None else terminal_since
            if now - terminal_since >= settings.terminal_grace_seconds:
                yield format_message(
                    "end", {"run_id": run_id, "status": batch.status.value}
                )
                return
        if now - started >= settings.max_seconds:
            return
        if now - last_sent >= settings.heartbeat_seconds:
            yield b": keepalive\n\n"
            last_sent = now
        if batch.caught_up or not batch.events:
            await asyncio.sleep(settings.poll_seconds)
        if await disconnected():
            return
        try:
            batch = await conversations.events(
                principal,
                run_id,
                after_event_id=cursor,
                after_sequence=sequence,
            )
        except Exception as error:
            api = to_api_error(error)
            yield format_message("error", {"code": api.code, "message": api.message})
            return
