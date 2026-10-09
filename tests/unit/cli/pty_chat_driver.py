"""Runs ``analytics chat`` against a paced fake backend (child of the PTY test).

Run 1 streams progress slowly, asks a clarification and completes after the
answer arrives; run 2 keeps working until it is cancelled. The test drives
the terminal and reads the screen; this process only plays the server.
"""

from __future__ import annotations

import json
import sys
import termios
import threading
import time
from collections.abc import Iterator
from typing import Any

import httpx

from retail_analytics.interfaces.cli.app import cli
from tests.unit.cli.fake_backend import Backend, event, run_view

answered = threading.Event()
cancelled = threading.Event()


def frame(e: dict[str, Any]) -> bytes:
    return (
        f"id: {e['event_id']}\nevent: {e['kind']}\ndata: {json.dumps(e)}\n\n".encode()
    )


def end(run_id: str, status: str) -> bytes:
    data = json.dumps({"run_id": run_id, "status": status})
    return f"event: end\ndata: {data}\n\n".encode()


class Paced(httpx.SyncByteStream):
    def __init__(self, parts: Iterator[bytes]) -> None:
        self.parts = parts

    def __iter__(self) -> Iterator[bytes]:
        yield from self.parts


def first_run() -> Iterator[bytes]:
    yield frame(event(1, "run.started", "Started."))
    time.sleep(1.5)
    yield frame(event(2, "tool.started", "Running a query."))
    time.sleep(1.5)
    yield frame(event(3, "input.required", "A question for you."))
    answered.wait(30)
    yield frame(event(4, "tool.started", "Checking the evidence."))
    time.sleep(0.3)
    yield frame(event(5, "run.completed", "Done."))
    yield end("r1", "completed")


def second_run() -> Iterator[bytes]:
    yield frame(event(1, "run.started", "Started the second run."))
    cancelled.wait(60)


def after_cancel() -> Iterator[bytes]:
    cancelled.wait(60)
    yield frame(event(2, "run.cancelled", "Run cancelled."))
    yield end("r2", "cancelled")


def main() -> None:
    import fcntl

    # Started in a new session: take this terminal as the controlling one so
    # Ctrl-C typed by the test arrives as SIGINT.
    fcntl.ioctl(sys.stdin.fileno(), termios.TIOCSCTTY, 0)
    backend = Backend()
    backend.runs["r1"] = run_view(
        "waiting_for_input", answer=None, question=("q1", "Which period?")
    )
    backend.runs["r2"] = run_view("running", answer=None, run_id="r2")
    backend.streams = [
        lambda r: httpx.Response(200, stream=Paced(first_run())),
        lambda r: httpx.Response(200, stream=Paced(second_run())),
        lambda r: httpx.Response(200, stream=Paced(after_cancel())),
    ]
    started = iter(["r1", "r2"])
    backend.overrides[("POST", "/v1/sessions")] = lambda r: httpx.Response(
        201,
        json={
            "session_id": "s1",
            "created_at": "2026-10-09T10:00:00Z",
            "last_activity_at": "2026-10-09T10:00:00Z",
        },
    )
    backend.overrides[("POST", "/v1/sessions/s1/messages")] = lambda r: httpx.Response(
        202, json={"input_id": "i1", "kind": "request", "run_id": next(started)}
    )

    def answer(request: httpx.Request) -> httpx.Response:
        if backend.json_body(request).get("text") != "last month":
            return httpx.Response(400, json={"error": {"code": "bad", "message": "x"}})
        backend.runs["r1"] = run_view("completed", answer="Revenue was 10.")
        answered.set()
        return httpx.Response(202, json={"input_id": "i2", "kind": "answer"})

    def cancel(request: httpx.Request) -> httpx.Response:
        backend.runs["r2"] = run_view("cancelled", answer=None, run_id="r2")
        cancelled.set()
        return httpx.Response(200, json={"run": backend.runs["r2"]})

    backend.overrides[("POST", "/v1/runs/r1/answers")] = answer
    backend.overrides[("POST", "/v1/runs/r2/cancel")] = cancel
    try:
        cli.main(["chat"], obj=backend.client, standalone_mode=False)
    finally:
        mode = termios.tcgetattr(sys.stdin.fileno())
        restored = bool(mode[3] & termios.ICANON and mode[3] & termios.ECHO)
        sys.stdout.write(f"\nTERMINAL-RESTORED={restored}\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
