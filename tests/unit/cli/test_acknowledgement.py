"""Every new chat message is acknowledged at once, from the server's receipt.

"Working on it." used to appear only when the run's ``run.started`` event
arrived over the stream. A run whose events arrive late (a slow start, a
stream that needs a reconnect) showed nothing until then, and a queued request
that finished before the chat looked for it was never shown at all. The
chat now says what the server accepted (starting, queued, steering, answer)
as soon as the message is accepted, and shows the start only once.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Any

import httpx

from retail_analytics.interfaces.cli.chat import Chat
from retail_analytics.interfaces.cli.client import ApiClient
from tests.unit.cli.fake_backend import Backend, error, event, run_view, sse
from tests.unit.cli.test_chat import base, chat, finishing_stream

WORKING = "Working on it."


def session_runs(*runs: tuple[str, str]) -> dict[str, Any]:
    return {
        "session_id": "s1",
        "created_at": "x",
        "last_activity_at": "x",
        "runs": [
            {
                "run_id": run_id,
                "session_id": "s1",
                "status": status,
                "active": status in ("running", "waiting_for_input"),
                "created_at": "x",
                "updated_at": "x",
                "completed_at": None,
            }
            for run_id, status in runs
        ],
    }


class Paced(httpx.SyncByteStream):
    def __init__(self, parts: Iterator[bytes]) -> None:
        self.parts = parts

    def __iter__(self) -> Iterator[bytes]:
        yield from self.parts


class Lines:
    """Interactive stdin: each line is released only when the test says so."""

    def __init__(self) -> None:
        self._lines: list[str] = []
        self._ready = threading.Condition()

    def send(self, line: str) -> None:
        with self._ready:
            self._lines.append(line + "\n")
            self._ready.notify_all()

    def readline(self) -> str:
        with self._ready:
            self._ready.wait_for(lambda: bool(self._lines), timeout=20)
            return self._lines.pop(0) if self._lines else ""


def interactive_chat(backend: Backend, lines: Lines) -> tuple[Chat, list[str]]:
    out: list[str] = []
    chat = Chat(
        ApiClient(backend.client()),
        "s1",
        out=out.append,
        stdin=lines,  # type: ignore[arg-type]
        interactive=True,
        sleep=_short_pause,
    )
    return chat, out


def _short_pause(_seconds: float) -> None:
    threading.Event().wait(0.02)


def late_start_stream() -> Any:
    """The first connection drops before any event (the run had not begun
    yet); the reconnect then delivers the whole run."""

    def first(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("connection reset", request=request)

    return [first, finishing_stream()]


def test_first_message_is_acknowledged_before_any_event_and_only_once() -> None:
    backend = base()
    backend.runs["r1"] = run_view("completed", answer="Revenue was 10.")
    backend.streams = [finishing_stream()]
    result = chat(backend, "How much revenue?\n/quit\n")
    assert result.exit_code == 0, result.output
    out = result.output
    assert out.count(WORKING) == 1  # not again when run.started arrives
    assert out.index(WORKING) < out.index("Running a query.")
    assert "\x1b" not in out and "\r" not in out


def test_acknowledgement_does_not_wait_for_a_late_run_started_event() -> None:
    # Reproduces the missing acknowledgement: the stream's first connection
    # delivered nothing, so the start was shown only after a reconnect (or
    # never, when the follower gave up); the receipt already said it started.
    backend = base()
    backend.runs["r1"] = run_view("completed", answer="Revenue was 10.")
    backend.streams = late_start_stream()
    result = chat(backend, "How much revenue?\n/quit\n")
    out = result.output
    assert WORKING in out
    assert out.index(WORKING) < out.index("connection lost")
    assert out.count(WORKING) == 1


def test_every_follow_up_message_is_acknowledged_once() -> None:
    backend = base()
    started = iter(["r1", "r2"])
    backend.overrides[("POST", "/v1/sessions/s1/messages")] = lambda r: httpx.Response(
        202, json={"input_id": "i", "kind": "request", "run_id": next(started)}
    )
    backend.runs["r1"] = run_view("completed", answer="Revenue was 10.")
    backend.runs["r2"] = run_view("completed", answer="By state: 4.", run_id="r2")
    backend.streams = [finishing_stream(), finishing_stream()]
    result = chat(backend, "How much revenue?\nand by state?\n/quit\n")
    out = result.output
    assert out.count(WORKING) == 2
    second = out.index("Revenue was 10.")
    assert WORKING in out[second:]
    assert out.index("By state: 4.") > out.index(WORKING, second)


def test_a_queued_request_that_already_finished_is_still_shown() -> None:
    # Before: the chat only followed a queued request whose run was still
    # active when it looked, so a quick one was reported as "did not start"
    # and its start and answer were never shown.
    backend = base()
    backend.runs["r1"] = run_view("completed", answer="Revenue was 10.")
    backend.runs["r2"] = run_view("completed", answer="Queued answer.", run_id="r2")
    queued = threading.Event()

    def first_run() -> Iterator[bytes]:
        yield sse([event(1, "run.started", "Started.")])
        queued.wait(20)
        yield sse([event(2, "run.completed", "Done.")], end="completed")

    backend.streams = [
        lambda r: httpx.Response(200, stream=Paced(first_run())),
        lambda r: httpx.Response(
            200,
            content=sse(
                [event(1, "run.started", "Go."), event(2, "run.completed", "Done.")],
                end="completed",
            ),
        ),
    ]

    def message(request: httpx.Request) -> httpx.Response:
        if backend.json_body(request)["mode"] == "queue":
            queued.set()
            return httpx.Response(
                202, json={"input_id": "i2", "kind": "queued", "run_id": None}
            )
        return httpx.Response(
            202, json={"input_id": "i1", "kind": "request", "run_id": "r1"}
        )

    backend.overrides[("POST", "/v1/sessions/s1/messages")] = message
    # By the time the chat looks, the queued run has already completed.
    backend.overrides[("GET", "/v1/sessions/s1")] = lambda r: httpx.Response(
        200, json=session_runs(("r2", "completed"), ("r1", "completed"))
    )
    lines = Lines()
    chat_, out = interactive_chat(backend, lines)
    worker = threading.Thread(target=chat_.run, kwargs={"resume": False})
    worker.start()
    lines.send("How much revenue?")
    lines.send("/queue and by state?")
    _wait_for(out, "Queued answer.")
    lines.send("/quit")
    worker.join(20)
    text = "\n".join(out)
    assert "did not start" not in text
    assert text.count("Queued: it will run after the current investigation.") == 1
    assert "Your queued question already ran:" in text
    assert text.count(WORKING) == 2  # the first request, then the queued one
    assert text.index("Revenue was 10.") < text.index("Queued answer.")


def test_steering_that_meets_a_finished_run_keeps_its_answer() -> None:
    # A message typed as the run finished: the steer is refused, the chat
    # starts a new request, and the finished run's answer is still shown.
    backend = base()
    backend.runs["r1"] = run_view("completed", answer="Revenue was 10.")
    backend.runs["r2"] = run_view("completed", answer="By state: 4.", run_id="r2")
    finish = threading.Event()

    def first_run() -> Iterator[bytes]:
        yield sse([event(1, "run.started", "Started.")])
        finish.wait(20)
        yield sse([event(2, "run.completed", "Done.")], end="completed")

    def steer(request: httpx.Request) -> httpx.Response:
        finish.set()
        return error(409, "run_not_active", "The run has finished.")

    started = iter(["r1", "r2"])
    backend.overrides[("POST", "/v1/sessions/s1/messages")] = lambda r: httpx.Response(
        202, json={"input_id": "i", "kind": "request", "run_id": next(started)}
    )
    backend.overrides[("POST", "/v1/runs/r1/steer")] = steer
    backend.streams = [
        lambda r: httpx.Response(200, stream=Paced(first_run())),
        finishing_stream("By state: 4."),
    ]
    lines = Lines()
    chat_, out = interactive_chat(backend, lines)
    worker = threading.Thread(target=chat_.run, kwargs={"resume": False})
    worker.start()
    lines.send("How much revenue?")
    _wait_for(out, WORKING)
    lines.send("and by state?")
    _wait_for(out, "By state: 4.")
    lines.send("/quit")
    worker.join(20)
    text = "\n".join(out)
    assert text.index("Revenue was 10.") < text.index("starting a new request")
    assert text.count(WORKING) == 2


def test_steering_and_answers_are_acknowledged_truthfully() -> None:
    backend = base()
    backend.runs["r1"] = run_view(
        "waiting_for_input", answer=None, question=("q1", "Which period?")
    )
    asked = threading.Event()
    answered = threading.Event()

    def run() -> Iterator[bytes]:
        yield sse([event(1, "run.started", "Started.")])
        asked.wait(20)
        yield sse(
            [
                event(
                    2,
                    "input.required",
                    "Waiting.",
                    input_request={"question_id": "q1", "question": "Which period?"},
                )
            ]
        )
        answered.wait(20)
        yield sse([event(3, "run.completed", "Done.")], end="completed")

    def steer(request: httpx.Request) -> httpx.Response:
        asked.set()
        return httpx.Response(
            202, json={"input_id": "i2", "kind": "steering", "run_id": "r1"}
        )

    def answer(request: httpx.Request) -> httpx.Response:
        backend.runs["r1"] = run_view("completed", answer="Last month: 10.")
        answered.set()
        return httpx.Response(
            202, json={"input_id": "i3", "kind": "answer", "run_id": "r1"}
        )

    backend.overrides[("POST", "/v1/runs/r1/steer")] = steer
    backend.overrides[("POST", "/v1/runs/r1/answers")] = answer
    backend.streams = [lambda r: httpx.Response(200, stream=Paced(run()))]
    lines = Lines()
    chat_, out = interactive_chat(backend, lines)
    worker = threading.Thread(target=chat_.run, kwargs={"resume": False})
    worker.start()
    lines.send("sales?")
    _wait_for(out, WORKING)
    lines.send("only women's products")
    _wait_for(out, "Type your answer below.")
    lines.send("last month")
    _wait_for(out, "Last month: 10.")
    lines.send("/quit")
    worker.join(20)
    text = "\n".join(out)
    assert text.count(WORKING) == 1
    assert "Sent as steering for the active run." in text
    assert text.index("Answer sent; the investigation continues.") < text.index(
        "Last month: 10."
    )


def test_resuming_shows_the_run_start_once_from_its_history() -> None:
    backend = Backend()
    backend.overrides[("GET", "/v1/sessions/s1")] = lambda r: httpx.Response(
        200, json=session_runs(("r1", "running"))
    )
    backend.runs["r1"] = run_view("running", answer=None)

    def finished(request: httpx.Request) -> httpx.Response:
        backend.runs["r1"] = run_view("completed", answer="Revenue was 10.")
        response: httpx.Response = finishing_stream()(request)
        return response

    backend.streams = [finished]
    result = chat(backend, "/quit\n", "--session", "s1")
    assert "Resuming run r1" in result.output
    assert result.output.count(WORKING) == 1
    assert "Revenue was 10." in result.output


def _wait_for(out: list[str], needle: str, seconds: float = 20) -> None:
    done = threading.Event()
    deadline = seconds
    while deadline > 0 and not any(needle in line for line in list(out)):
        done.wait(0.05)
        deadline -= 0.05
    assert any(needle in line for line in out), (needle, out)
