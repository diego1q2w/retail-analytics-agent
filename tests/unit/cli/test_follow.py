from __future__ import annotations

import httpx
import pytest

from retail_analytics.interfaces.cli.client import ApiClient, ApiError
from retail_analytics.interfaces.cli.follow import StreamLost, follow_run
from retail_analytics.interfaces.cli.sse import (
    SseComment,
    SseMessage,
    SseRetry,
    parse_sse,
)
from tests.unit.cli.fake_backend import (
    Backend,
    BrokenStream,
    error,
    event,
    run_view,
    sse,
)


def test_sse_parser_reads_messages_comments_and_retry() -> None:
    raw = (
        "retry: 2000\n: keepalive\nid: a\nevent: x\ndata: {}\n\nevent: end\ndata: 1\n\n"
    )
    assert list(parse_sse(raw.splitlines(keepends=True))) == [
        SseRetry(2000),
        SseComment(),
        SseMessage("x", "{}", "a"),
        SseMessage("end", "1", None),
    ]


def make(backend: Backend) -> ApiClient:
    return ApiClient(backend.client(), sleep=lambda _s: None)


def test_reconnect_resumes_from_last_event_id_without_gaps_or_repeats() -> None:
    backend = Backend()
    first = [event(1, "run.started", "go"), event(2, "tool.started", "query")]
    # The second connection (wrongly) replays event 2; it must not show twice.
    second = [event(2, "tool.started", "query"), event(3, "tool.succeeded", "done")]
    backend.streams = [
        lambda r: httpx.Response(200, stream=BrokenStream(sse(first))),
        lambda r: httpx.Response(200, content=sse(second, end="completed")),
    ]
    seen: list[int] = []
    notices: list[str] = []
    result = follow_run(
        make(backend),
        "r1",
        on_event=lambda e: seen.append(e["sequence"]),
        on_notice=notices.append,
        sleep=lambda _s: None,
    )
    assert seen == [1, 2, 3]
    assert result.outcome == "end" and result.status == "completed"
    streams = backend.calls("GET", "/events")
    assert "last-event-id" not in streams[0].headers
    assert streams[1].headers["Last-Event-ID"] == "ev2"
    assert notices and "reconnecting" in notices[0]


def test_stream_closed_without_end_reconnects_and_gives_up_eventually() -> None:
    backend = Backend()
    backend.streams = [
        lambda r: httpx.Response(200, content=sse([event(1, "run.started", "go")]))
    ] + [lambda r: httpx.Response(200, content=b"") for _ in range(3)]
    with pytest.raises(StreamLost) as lost:
        follow_run(
            make(backend),
            "r1",
            on_event=lambda e: None,
            max_failures=2,
            sleep=lambda _s: None,
        )
    assert lost.value.last_event_id == "ev1"


def test_error_event_and_http_errors_are_structured() -> None:
    backend = Backend()
    backend.streams = [
        lambda r: httpx.Response(
            200,
            content=b'event: error\ndata: {"code": "not_found", "message": "gone"}\n\n',
        ),
        lambda r: error(404, "not_found", "No such run."),
    ]
    with pytest.raises(ApiError) as first:
        follow_run(make(backend), "r1", on_event=lambda e: None)
    assert first.value.code == "not_found"
    with pytest.raises(ApiError) as second:
        follow_run(make(backend), "r1", on_event=lambda e: None)
    assert second.value.status == 404


def test_keepalive_comments_do_not_break_the_stream() -> None:
    backend = Backend()
    body = b": keepalive\n\n" + sse([event(1, "run.started", "go")], end="completed")
    backend.streams = [lambda r: httpx.Response(200, content=body)]
    seen: list[int] = []
    follow_run(make(backend), "r1", on_event=lambda e: seen.append(e["sequence"]))
    assert seen == [1]
    backend.runs["r1"] = run_view()
