"""Follow a run's event stream, reconnecting with ``Last-Event-ID``.

Disconnecting never cancels anything. After a dropped connection, a stall (no
event or keepalive within ``stall_seconds``) or the server's own maximum
connection time, the follower reconnects with the ID of the last event it
handled, so the later events arrive in order with no gaps or repeats. Events
already handled (by sequence) are skipped defensively.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from retail_analytics.interfaces.cli.client import (
    ApiClient,
    ApiError,
    JsonObject,
    error_from_response,
)
from retail_analytics.interfaces.cli.sse import (
    SseComment,
    SseMessage,
    SseRetry,
    parse_sse,
)

DEFAULT_STALL_SECONDS = 45.0  # the server sends a keepalive every 15 seconds
DEFAULT_MAX_FAILURES = 8
# A connection that has stayed open this long before a keepalive arrives is
# healthy (a quiet investigation); an instant "preamble then close" is not.
HEALTHY_CONNECTION_SECONDS = 5.0


class StopFollowing(Exception):
    """Raised by an ``on_event`` callback to stop after the current event."""


class StreamLost(Exception):
    """Reconnecting did not work; ``last_event_id`` resumes later."""

    def __init__(self, last_event_id: str | None) -> None:
        self.last_event_id = last_event_id
        super().__init__("event stream lost")


@dataclass(frozen=True, slots=True)
class FollowResult:
    # "end" (run finished, status from the end message), "stopped" (caller asked).
    outcome: str
    status: str | None
    last_event_id: str | None
    last_sequence: int


def follow_run(
    api: ApiClient,
    run_id: str,
    *,
    after: str | None = None,
    on_event: Callable[[JsonObject], None],
    on_notice: Callable[[str], None] = lambda _text: None,
    should_stop: Callable[[], bool] = lambda: False,
    stall_seconds: float = DEFAULT_STALL_SECONDS,
    max_failures: int = DEFAULT_MAX_FAILURES,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> FollowResult:
    """Follow a run until its end message.

    Meaningful progress resets the consecutive-failure allowance. It is only:
    a fresh event handled (or an end/error message), or a keepalive comment
    that arrives after the connection has stayed open for at least
    ``HEALTHY_CONNECTION_SECONDS``. The ``retry:`` directive the server sends
    at the start of every connection, and a keepalive right after connecting,
    prove nothing: a server that keeps closing right after that preamble must
    still run out of attempts and raise ``StreamLost``.
    """
    api.require_token()
    last_id = after
    last_sequence = 0
    failures = 0
    retry_seconds = 2.0
    timeout = httpx.Timeout(api.http.timeout.connect, read=stall_seconds)
    path = f"/v1/runs/{quote(run_id)}/events"
    while True:
        if should_stop():
            return FollowResult("stopped", None, last_id, last_sequence)
        headers = {} if last_id is None else {"Last-Event-ID": last_id}
        try:
            connected_at = monotonic()
            with api.http.stream(
                "GET", path, headers=headers, timeout=timeout
            ) as response:
                if response.status_code >= 400:
                    response.read()
                    error = error_from_response(response)
                    if error.status == 503:
                        raise httpx.ConnectError("unavailable")
                    raise error
                for item in parse_sse(response.iter_lines()):
                    if should_stop():
                        return FollowResult("stopped", None, last_id, last_sequence)
                    if isinstance(item, SseRetry):
                        retry_seconds = min(max(item.milliseconds / 1000, 0.2), 10.0)
                        continue
                    if isinstance(item, SseComment):
                        if monotonic() - connected_at >= HEALTHY_CONNECTION_SECONDS:
                            failures = 0
                        continue
                    try:
                        result = _handle(item, on_event, last_sequence)
                    except StopFollowing:
                        return FollowResult(
                            "stopped", None, item.id or last_id, last_sequence
                        )
                    if result is not None:
                        failures = 0
                    if isinstance(result, FollowResult):
                        return FollowResult(
                            "end", result.status, last_id, last_sequence
                        )
                    if result is not None:
                        last_sequence = result
                        last_id = item.id or last_id
        except httpx.TransportError as exc:
            failures += 1
            if failures > max_failures:
                raise StreamLost(last_id) from exc
            on_notice(
                f"connection lost ({type(exc).__name__}); reconnecting from the "
                "last event"
            )
            sleep(min(retry_seconds * failures, 10.0))
            continue
        # The server closed the stream without an end message (for example its
        # maximum connection time): resume from the last event.
        failures += 1
        if failures > max_failures:
            raise StreamLost(last_id)
        sleep(0.2)


def _handle(
    message: SseMessage,
    on_event: Callable[[JsonObject], None],
    last_sequence: int,
) -> FollowResult | int | None:
    """Return a FollowResult at the end, the new sequence for a fresh event,
    or None for an event to skip."""
    if message.event == "end":
        payload = _payload(message)
        return FollowResult("end", str(payload.get("status")), None, last_sequence)
    if message.event == "error":
        payload = _payload(message)
        raise ApiError(
            str(payload.get("code", "stream_error")),
            str(payload.get("message", "the event stream ended with an error")),
            details=payload.get("details") or {},
        )
    event = _payload(message)
    sequence = event.get("sequence")
    if not isinstance(sequence, int) or sequence <= last_sequence:
        return None
    on_event(event)
    return sequence


def _payload(message: SseMessage) -> dict[str, Any]:
    try:
        payload = json.loads(message.data)
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}
