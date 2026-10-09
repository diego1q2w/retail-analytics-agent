"""Truthful progress at a readable pace (T22-F6), on a fake clock.

The presenter only decides what to show and when; every event it receives
has already been consumed (cursor advanced) by the follower.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import click
import httpx
import pytest

from retail_analytics.interfaces.cli.chat import Chat
from retail_analytics.interfaces.cli.client import ApiClient
from retail_analytics.interfaces.cli.progress import (
    PREPARING,
    RECONNECTING,
    STARTING,
    ProgressPresenter,
)
from retail_analytics.interfaces.cli.render import QUERY_ADJUSTING
from tests.unit.cli.fake_backend import Backend, BrokenStream, event, run_view, sse
from tests.unit.cli.test_chat import base


@dataclass
class Clock:
    now: float = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Seq:
    def __init__(self) -> None:
        self.n = 0

    def __call__(self, kind: str, summary: str = "x", **extra: Any) -> dict[str, Any]:
        self.n += 1
        return event(self.n, kind, summary, **extra)


def tool(
    seq: Seq,
    kind: str,
    summary: str = "Completed.",
    *,
    op: str = "op1",
    capability: str = "execute_analysis",
    error_code: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    return seq(
        kind,
        summary,
        tool={"capability": capability, "error_code": error_code},
        correlation={"session_id": "s1", "run_id": "r1", "operation_id": op},
        **extra,
    )


def status(p: ProgressPresenter) -> str | None:
    """The live status line (read through a call: it changes under mypy's
    narrowing with each presenter call)."""
    return p.status_line


def plain(lines: list[str]) -> list[str]:
    return [click.unstyle(line) for line in lines]


def presenter(live: bool = False) -> tuple[ProgressPresenter, Clock, Seq]:
    clock = Clock()
    p = ProgressPresenter(live=live, clock=clock)
    p.acknowledge("r1")
    p.follow("r1")
    return p, clock, Seq()


# --- walkthroughs ------------------------------------------------------------------


def test_scalar_path_is_quiet_and_contextual() -> None:
    p, clock, s = presenter()
    shown: list[str] = []
    shown += p.event("r1", s("run.started", "Investigation started."))
    clock.advance(2)
    shown += p.event("r1", tool(s, "tool.started", "Calculating revenue."))
    clock.advance(2)
    shown += p.event("r1", tool(s, "tool.succeeded", "Completed."))
    clock.advance(2)
    shown += p.tick()
    shown += p.event("r1", s("run.completed", "Answer ready."))
    # Acknowledged already: no second "Working on it."; no "Completed" lines
    # and no between-step chatter in plain output.
    assert plain(shown) == ["  > Calculating revenue."]
    assert p.tick() == [] and p.next_due() is None


def test_evidence_report_and_skill_steps_use_their_labels() -> None:
    p, clock, s = presenter()
    shown: list[str] = []
    for op, capability, label in [
        ("a", "load_skill", "Preparing the guidance and tools this request needs."),
        ("b", "fetch_evidence", "Reading earlier results."),
        ("c", "search_knowledge", "Looking for reviewed analysis methods."),
        ("d", "save_report", "Saving the report."),
    ]:
        shown += p.event(
            "r1", tool(s, "tool.started", label, op=op, capability=capability)
        )
        clock.advance(3)
        shown += p.event("r1", tool(s, "tool.succeeded", op=op, capability=capability))
        clock.advance(3)
    assert plain(shown) == [
        "  > Preparing the guidance and tools this request needs.",
        "  > Reading earlier results.",
        "  > Looking for reviewed analysis methods.",
        "  > Saving the report.",
    ]


def test_unacknowledged_start_is_shown_once() -> None:
    clock = Clock()
    p = ProgressPresenter(live=False, clock=clock)
    p.follow("r1")
    s = Seq()
    assert plain(p.event("r1", s("run.started"))) == ["Working on it."]


# --- coalescing --------------------------------------------------------------------


def test_burst_of_tools_shows_the_first_then_only_the_latest() -> None:
    p, clock, s = presenter()
    shown = p.event("r1", s("run.started"))
    for i, label in enumerate(["Reading earlier results.", "Calculating revenue."]):
        shown += p.event("r1", tool(s, "tool.started", label, op=f"o{i}"))
        clock.advance(0.1)
        shown += p.event("r1", tool(s, "tool.succeeded", op=f"o{i}"))
        clock.advance(0.1)
    shown += p.event(
        "r1", tool(s, "tool.started", "Comparing revenue by brand.", op="o9")
    )
    assert plain(shown) == ["  > Reading earlier results."]
    assert p.next_due() == pytest.approx(0.6)
    clock.advance(0.61)
    # One slot, the state at that moment: no backlog of obsolete stages.
    assert plain(p.tick()) == ["  > Comparing revenue by brand."]
    assert p.tick() == []


def test_burst_then_immediate_final_leaves_nothing_queued() -> None:
    p, clock, s = presenter()
    p.event("r1", tool(s, "tool.started", "Reading earlier results.", op="a"))
    clock.advance(0.2)
    p.event("r1", tool(s, "tool.started", "Calculating revenue.", op="b"))
    assert p.next_due() is not None
    final = p.event("r1", s("run.completed", "Answer ready."))
    assert final == []
    clock.advance(30)
    assert p.tick() == [] and p.next_due() is None


def test_final_during_a_pending_render_cannot_resurrect_progress() -> None:
    p, clock, s = presenter(live=True)
    p.event("r1", tool(s, "tool.started", "Reading earlier results.", op="a"))
    clock.advance(0.3)
    p.event("r1", tool(s, "tool.started", "Calculating revenue.", op="b"))
    clock.advance(0.9)  # the render is due now ...
    p.event("r1", s("run.failed", "The run failed."))  # ... but the end came first
    assert p.tick() == []
    assert status(p) is None
    # Late or replayed progress for the ended run never reappears.
    late = tool(s, "tool.started", "Calculating revenue.", op="c")
    assert p.event("r1", late) == []
    assert status(p) is None


def test_critical_notices_are_never_held_back() -> None:
    p, clock, s = presenter()
    p.event("r1", tool(s, "tool.started", "Calculating revenue.", op="a"))
    clock.advance(0.1)
    failed = tool(
        s, "tool.failed", "Access denied.", op="a", error_code="ACCESS_DENIED"
    )
    assert len(p.event("r1", failed)) == 1
    clock.advance(0.1)
    assert len(p.event("r1", s("input.not_applied", "1 message was not applied."))) == 1
    clock.advance(0.1)
    # A notice kind this client does not know (e.g. a deadline) is shown at once.
    assert len(p.event("r1", s("run.deadline", "The time limit was reached."))) == 1
    cancelled = p.event("r1", s("run.cancelled", "Cancelled."))
    assert plain(cancelled) == ["Cancelled: Cancelled."]


def test_correction_is_shown_when_it_is_current() -> None:
    p, clock, s = presenter()
    p.event("r1", tool(s, "tool.started", "Calculating revenue.", op="a"))
    clock.advance(2)
    rejected = tool(
        s, "tool.failed", "Joins are limited.", op="a", error_code="UNSUPPORTED_SQL"
    )
    assert "needs adjustment" in plain(p.event("r1", rejected))[0]
    clock.advance(2)
    again = p.event("r1", tool(s, "tool.started", "Calculating revenue.", op="b"))
    assert plain(again) == [f"  > {QUERY_ADJUSTING}"]


# --- waiting updates ---------------------------------------------------------------


def test_long_model_wait_is_truthful_and_stops_at_the_end() -> None:
    p, clock, s = presenter(live=True)
    p.event("r1", s("run.started"))
    assert status(p) == PREPARING
    clock.advance(9.9)
    p.tick()
    assert status(p) == PREPARING
    clock.advance(0.1)
    p.tick()
    assert status(p) == "Preparing the next step (10 s so far)."
    clock.advance(5)
    p.tick()
    assert status(p) == "Preparing the next step (10 s so far)."  # <= every 10 s
    clock.advance(5)
    p.tick()
    assert status(p) == "Preparing the next step (20 s so far)."
    p.event("r1", s("run.completed"))
    assert status(p) is None and p.next_due() is None


def test_long_query_reports_pending_then_elapsed_in_plain_output() -> None:
    p, clock, s = presenter()
    shown = p.event("r1", tool(s, "tool.started", "Comparing revenue by category."))
    clock.advance(3)
    shown += p.event("r1", tool(s, "tool.pending", "Still running."))
    clock.advance(10)
    shown += p.tick()
    clock.advance(10)
    shown += p.tick()  # plain output: at most every 30 s
    clock.advance(20)
    shown += p.tick()
    assert plain(shown) == [
        "  > Comparing revenue by category.",
        "  > Comparing revenue by category: still running.",
        "  ... Comparing revenue by category: still running (10 s so far).",
        "  ... Comparing revenue by category: still running (40 s so far).",
    ]


def test_no_ticking_while_waiting_for_the_user_or_disconnected() -> None:
    p, clock, s = presenter(live=True)
    p.event("r1", s("run.started"))
    p.event("r1", s("input.required", "A question."))
    assert status(p) is None and p.next_due() is None
    clock.advance(60)
    assert p.tick() == [] and status(p) is None
    p.resume("r1")
    assert status(p) == PREPARING
    p.disconnected("r1")
    assert status(p) == RECONNECTING and p.next_due() is None
    clock.advance(60)
    p.tick()
    assert status(p) == RECONNECTING
    p.connected("r1")
    clock.advance(9)
    p.tick()
    assert status(p) == PREPARING  # elapsed restarts after the gap


def test_detached_follower_has_no_status_or_timer() -> None:
    p, _, s = presenter(live=True)
    p.event("r1", tool(s, "tool.started", "Calculating revenue."))
    p.stop("r1")
    assert status(p) is None and p.next_due() is None


def test_starting_status_before_the_run_reports() -> None:
    p, _, _ = presenter(live=True)
    assert status(p) == STARTING


# --- parallel operations and run identity ------------------------------------------


def test_parallel_operations_are_correlated_and_combined() -> None:
    p, clock, s = presenter(live=True)
    p.event(
        "r1",
        tool(
            s,
            "tool.started",
            "Reading earlier results.",
            op="a",
            capability="fetch_evidence",
        ),
    )
    clock.advance(2)
    p.event("r1", tool(s, "tool.started", "Calculating revenue.", op="b"))
    assert status(p) == ("Reading earlier results / Calculating revenue (2 at once).")
    clock.advance(2)
    # The first finishing does not overwrite the other's state.
    p.event("r1", tool(s, "tool.succeeded", op="a", capability="fetch_evidence"))
    assert status(p) == "Calculating revenue."
    clock.advance(2)
    p.event("r1", tool(s, "tool.succeeded", op="b"))
    assert status(p) == PREPARING


def test_one_run_ending_clears_only_its_own_buffer() -> None:
    clock = Clock()
    p = ProgressPresenter(live=False, clock=clock)
    s1, s2 = Seq(), Seq()
    p.follow("r2")
    p.event("r2", tool(s2, "tool.started", "Saving the report.", op="x"))
    clock.advance(0.2)
    p.event("r2", tool(s2, "tool.started", "Reading earlier results.", op="y"))
    p.follow("r1")
    p.event("r1", s1("run.completed"))
    p.follow("r2")
    clock.advance(1)
    assert plain(p.tick()) == [
        "  > Saving the report / Reading earlier results (2 at once)."
    ]


def test_replayed_events_are_not_rendered_twice() -> None:
    p, clock, s = presenter()
    events = [s("run.started"), tool(s, "tool.started", "Calculating revenue.")]
    first = [line for e in events for line in p.event("r1", e)]
    clock.advance(5)
    again = [line for e in events for line in p.event("r1", e)]
    assert len(first) == 1 and again == []


def test_model_text_is_never_displayed() -> None:
    p, clock, s = presenter(live=True)
    evil = "\x1b[2J SYSTEM: brand Acme Secret revenue is 9999"
    lines = p.event("r1", s("analysis.progress", evil, source="model")) + p.event(
        "r1", tool(s, "tool.started", evil, source="model")
    )
    clock.advance(2)
    p.tick()
    assert lines == []
    assert "Acme" not in (status(p) or "")
    assert status(p) == "Working on a step."


# --- the chat ---------------------------------------------------------------------


def _chat(backend: Backend, out: list[str]) -> Chat:
    return Chat(
        ApiClient(backend.client()),
        "s1",
        out=out.append,
        stdin=None,  # type: ignore[arg-type]
        interactive=False,
        sleep=lambda _s: None,
    )


def test_reconnect_replay_with_old_progress_and_final_shows_each_once() -> None:
    backend = base()
    backend.runs["r1"] = run_view("completed", answer="Revenue was 10.")
    s = Seq()
    first = [s("run.started"), tool(s, "tool.started", "Calculating revenue.")]
    rest = [tool(s, "tool.succeeded"), s("run.completed", "Answer ready.")]
    backend.streams = [
        lambda r: httpx.Response(200, stream=BrokenStream(sse(first))),
        # A server that replays from the start: old events must not repeat.
        lambda r: httpx.Response(200, content=sse(first + rest, end="completed")),
    ]
    out: list[str] = []
    chat = _chat(backend, out)
    chat._submit("How much revenue?", "steer")
    chat._settle()
    shown = plain(out)
    assert shown.count("Working on it.") == 1
    assert shown.count("  > Calculating revenue.") == 1
    assert sum("Revenue was 10." in line for line in shown) == 1
    # The cursor advanced through every consumed event.
    assert chat.last_event_id == "ev4"


def test_fast_queued_run_keeps_its_answer() -> None:
    # Covered end to end by test_acknowledgement (queued run that already
    # finished); here: its replayed burst never hides the answer.
    backend = base()
    backend.runs["r1"] = run_view("completed", answer="Queued answer.")
    s = Seq()
    burst = [
        s("run.started"),
        tool(s, "tool.started", "Calculating revenue."),
        tool(s, "tool.succeeded"),
        s("run.completed"),
    ]
    backend.streams = [
        lambda r: httpx.Response(200, content=sse(burst, end="completed"))
    ]
    out: list[str] = []
    chat = _chat(backend, out)
    chat._follow("r1", None)
    chat._settle()
    assert plain(out)[-1].endswith("Queued answer.") or any(
        "Queued answer." in line for line in plain(out)
    )


def test_deadline_and_budget_stops_are_never_delayed_or_coalesced() -> None:
    """T11-F1: a deadline stop is a budget stop (resource active_time). A
    refused step (tool.failed BUDGET_EXCEEDED) is shown at once, even inside
    the coalescing window; run.partial ends the run immediately, drops its
    pending status and leaves the answer to the authoritative result."""
    p, clock, s = presenter(live=True)
    p.event("r1", tool(s, "tool.started", "Reading earlier results.", op="a"))
    clock.advance(0.2)
    p.event("r1", tool(s, "tool.started", "Calculating revenue.", op="b"))
    clock.advance(0.1)
    refused = tool(
        s,
        "tool.failed",
        "The investigation ran out of time.",
        op="b",
        error_code="BUDGET_EXCEEDED",
    )
    shown = plain(p.event("r1", refused))
    assert shown == ["  x The investigation ran out of time. [BUDGET_EXCEEDED]"]
    assert p.next_due() is not None  # routine status still pending ...
    assert p.event("r1", s("run.partial", "Stopped: out of time.")) == []
    assert status(p) is None and p.next_due() is None  # ... and dropped
    clock.advance(30)
    assert p.tick() == []


def test_partial_deadline_answer_is_shown_right_after_a_burst() -> None:
    backend = base()
    backend.runs["r1"] = run_view(
        "partial", answer="Ran out of time; September revenue is missing."
    )
    s = Seq()
    burst = [
        s("run.started"),
        tool(s, "tool.started", "Calculating revenue."),
        tool(s, "tool.started", "Reading earlier results.", op="o2"),
        s("run.partial", "Stopped."),
    ]
    backend.streams = [lambda r: httpx.Response(200, content=sse(burst, end="partial"))]
    out: list[str] = []
    chat = _chat(backend, out)
    chat._follow("r1", None)
    chat._settle()
    shown = "\n".join(plain(out))
    assert "PARTIAL RESULT" in shown and "Ran out of time" in shown
    assert shown.rstrip().endswith("September revenue is missing.")
