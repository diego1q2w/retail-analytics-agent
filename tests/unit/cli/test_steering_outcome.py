"""The chat tells the truth about steering after acknowledging it: applied at
the run's next step, or not applied because the run ended first (then the
notice comes before the answer, which does not include the message)."""

from __future__ import annotations

import threading
from collections.abc import Iterator

import click
import httpx

from retail_analytics.domain.investigations import (
    APPLIED_SUMMARY,
    unapplied_summary,
)
from retail_analytics.interfaces.cli.render import STEERING_APPLIED, format_event
from tests.unit.cli.fake_backend import event, run_view, sse
from tests.unit.cli.test_acknowledgement import (
    WORKING,
    Lines,
    Paced,
    _wait_for,
    interactive_chat,
)
from tests.unit.cli.test_chat import base

STEERED = "Sent as steering for the active run."
NOTICE = (
    'Note: your message "use annual sales" arrived after this investigation\'s '
    "last step and was not applied to this answer."
)


def test_outcome_events_render_as_their_own_lines() -> None:
    applied = format_event({"kind": "input.applied", "summary": APPLIED_SUMMARY})
    assert applied is not None and STEERING_APPLIED in click.unstyle(applied)
    summary = unapplied_summary(1)
    shown = format_event({"kind": "input.not_applied", "summary": summary})
    assert shown is not None and click.unstyle(shown) == f"  ! {summary}"
    assert STEERING_APPLIED == APPLIED_SUMMARY


def _steered_chat(outcome: list[bytes], answer: str) -> str:
    backend = base()
    backend.runs["r1"] = run_view("running", answer=None)
    steered = threading.Event()

    def run() -> Iterator[bytes]:
        yield sse([event(1, "run.started", "Started.")])
        steered.wait(20)
        yield from outcome

    def steer(request: httpx.Request) -> httpx.Response:
        backend.runs["r1"] = run_view("partial", answer=answer)
        steered.set()
        return httpx.Response(
            202, json={"input_id": "i2", "kind": "steering", "run_id": "r1"}
        )

    backend.overrides[("POST", "/v1/runs/r1/steer")] = steer
    backend.streams = [lambda r: httpx.Response(200, stream=Paced(run()))]
    lines = Lines()
    chat_, out = interactive_chat(backend, lines)
    worker = threading.Thread(target=chat_.run, kwargs={"resume": False})
    worker.start()
    lines.send("sales?")
    _wait_for(out, WORKING)
    lines.send("use annual sales")
    _wait_for(out, answer.split("\n")[0])
    lines.send("/quit")
    worker.join(20)
    return click.unstyle("\n".join(out))


def test_steering_the_run_could_not_apply_is_said_before_the_answer() -> None:
    answer = f"Stopped. Model request limit reached.\n\n{NOTICE}"
    text = _steered_chat(
        [
            sse([event(2, "input.not_applied", unapplied_summary(1))]),
            sse([event(3, "run.partial", "Partial.")], end="partial"),
        ],
        answer,
    )
    assert text.index(STEERED) < text.index(unapplied_summary(1))
    assert text.index(unapplied_summary(1)) < text.index("Stopped. Model request")
    assert NOTICE in text
    assert STEERING_APPLIED not in text


def test_applied_steering_is_confirmed() -> None:
    text = _steered_chat(
        [
            sse([event(2, "input.applied", APPLIED_SUMMARY)]),
            sse([event(3, "run.completed", "Done.")], end="completed"),
        ],
        "Annual sales: 120.",
    )
    assert text.index(STEERED) < text.index(STEERING_APPLIED)
    assert text.index(STEERING_APPLIED) < text.index("Annual sales: 120.")
    assert "not applied" not in text
