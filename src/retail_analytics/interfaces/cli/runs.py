"""Driving one run to a stopping point: finished, or waiting for the user."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from retail_analytics.interfaces.cli.client import ApiClient, JsonObject
from retail_analytics.interfaces.cli.follow import (
    DEFAULT_STALL_SECONDS,
    StopFollowing,
    follow_run,
)
from retail_analytics.interfaces.cli.render import (
    EventFormatter,
    format_question,
    format_run_result,
)

Output = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class Question:
    question_id: str
    text: str


def open_question(
    api: ApiClient,
    run_id: str,
    *,
    tries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> Question | None:
    """The run's open clarification question, if it really is waiting now.

    The run record flips to waiting a moment after the event, so look a few
    times; an old, already answered question (seen when replaying history)
    is not open and is ignored.
    """
    for attempt in range(tries):
        run = api.get_run(run_id)
        question = run.get("question")
        if run.get("status") == "waiting_for_input" and isinstance(question, dict):
            text = question.get("text") or {}
            return Question(str(question["question_id"]), str(text.get("text", "")))
        if run.get("status") != "running":
            return None
        if attempt + 1 < tries:
            sleep(0.3)
    return None


@dataclass(frozen=True, slots=True)
class Driven:
    # "done" (terminal run), "waiting" (needs an answer) or "detached".
    state: str
    run: JsonObject
    question: Question | None = None
    last_event_id: str | None = None


def drive_run(
    api: ApiClient,
    run_id: str,
    *,
    out: Output,
    after: str | None = None,
    answers: Sequence[str] = (),
    ask: Callable[[Question], str | None] | None = None,
    key: str | None = None,
    quiet: bool = False,
    stall_seconds: float = DEFAULT_STALL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> Driven:
    """Follow ``run_id``; answer questions from ``answers`` then ``ask``;
    stop when the run ends or a question has no answer available."""
    pending = list(answers)
    format_event = EventFormatter()
    given = 0
    last_id = after
    while True:
        found: list[Question] = []

        def on_event(event: JsonObject, found: list[Question] = found) -> None:
            if not quiet:
                line = format_event(event)
                if line:
                    out(line)
            if event.get("kind") == "input.required":
                question = open_question(api, run_id, sleep=sleep)
                if question is not None:
                    found.append(question)
                    raise StopFollowing

        result = follow_run(
            api,
            run_id,
            after=last_id,
            on_event=on_event,
            on_notice=(lambda text: None) if quiet else out,
            stall_seconds=stall_seconds,
            sleep=sleep,
        )
        last_id = result.last_event_id
        if not found:
            run = api.get_run(run_id)
            if not quiet:
                out(format_run_result(run))
            return Driven("done", run, None, last_id)
        question = found[0]
        if not quiet:
            out(format_question(question.text))
        text = pending.pop(0) if pending else (ask(question) if ask else None)
        if text is None:
            return Driven("waiting", api.get_run(run_id), question, last_id)
        given += 1
        if not quiet:
            out(f"> {text}")
        api.answer(
            run_id,
            question.question_id,
            text,
            f"{key}-a{given}" if key else _fresh_key(),
        )


def _fresh_key() -> str:
    from retail_analytics.interfaces.cli.client import new_submission_key

    return new_submission_key()
