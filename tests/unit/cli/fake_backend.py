"""An in-memory stand-in for the HTTP API behind ``httpx.MockTransport``."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx

TOKEN = "tok-secret-value-123"


def event(sequence: int, kind: str, summary: str, **extra: Any) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": kind,
        "source": "application",
        "summary": summary,
        "tool": extra.pop("tool", None),
        "input_request": extra.pop("input_request", None),
        "event_id": f"ev{sequence}",
        "sequence": sequence,
        "occurred_at": "2026-10-09T10:00:00Z",
        **extra,
    }


def sse(events: list[dict[str, Any]], *, end: str | None = None) -> bytes:
    text = ""
    for e in events:
        text += f"id: {e['event_id']}\nevent: {e['kind']}\ndata: {json.dumps(e)}\n\n"
    if end:
        text += f"event: end\ndata: {json.dumps({'run_id': 'r1', 'status': end})}\n\n"
    return text.encode()


class BrokenStream(httpx.SyncByteStream):
    """Yields the bytes, then fails like a dropped connection."""

    def __init__(self, data: bytes) -> None:
        self.data = data

    def __iter__(self) -> Iterator[bytes]:
        yield self.data
        raise httpx.ReadError("connection reset")


@dataclass
class Backend:
    runs: dict[str, dict[str, Any]] = field(default_factory=dict)
    streams: list[Callable[[httpx.Request], httpx.Response]] = field(
        default_factory=list
    )
    requests: list[httpx.Request] = field(default_factory=list)
    overrides: dict[tuple[str, str], Callable[[httpx.Request], httpx.Response]] = field(
        default_factory=dict
    )

    def json_body(self, request: httpx.Request) -> dict[str, Any]:
        return json.loads(request.content) if request.content else {}

    def calls(self, method: str, suffix: str) -> list[httpx.Request]:
        return [
            r
            for r in self.requests
            if r.method == method and r.url.path.endswith(suffix)
        ]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if (request.method, path) in self.overrides:
            return self.overrides[(request.method, path)](request)
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return error(401, "unauthenticated", "A valid bearer token is required.")
        if path.endswith("/events"):
            if not self.streams:
                raise httpx.ConnectError("no more streams", request=request)
            return self.streams.pop(0)(request)
        if request.method == "GET" and path.startswith("/v1/runs/"):
            run_id = path.rsplit("/", 1)[-1]
            if run_id in self.runs:
                return httpx.Response(200, json=self.runs[run_id])
            return error(404, "not_found", "No such run.")
        return error(404, "not_found", f"unhandled {request.method} {path}")

    def client(self, token: str | None = TOKEN) -> httpx.Client:
        headers = {} if token is None else {"Authorization": f"Bearer {token}"}
        return httpx.Client(
            base_url="http://backend",
            transport=httpx.MockTransport(self.handle),
            headers=headers,
        )


def error(
    status: int, code: str, message: str, details: dict[str, Any] | None = None
) -> httpx.Response:
    return httpx.Response(
        status,
        json={"error": {"code": code, "message": message, "details": details or {}}},
    )


def run_view(
    status: str = "completed",
    *,
    answer: str | None = "Revenue was 10.",
    withheld: bool = False,
    question: tuple[str, str] | None = None,
    run_id: str = "r1",
    citations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "session_id": "s1",
        "status": status,
        "active": status in ("running", "waiting_for_input", "cancelling"),
        "created_at": "2026-10-09T10:00:00Z",
        "updated_at": "2026-10-09T10:01:00Z",
        "completed_at": None,
        "question": None
        if question is None
        else {
            "question_id": question[0],
            "text": {"text": question[1], "withheld": False},
        },
        "answer": None
        if answer is None
        else {"text": answer, "withheld": withheld, "citations": citations or []},
    }
