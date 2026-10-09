"""Truthful progress at a readable pace (presentation only).

The server's events are the only source: each one is a real state change of
the run (a tool started, finished, is still pending; the run asks, applies
steering or ends). This module decides how and when the CLI shows them; it
never changes the run, its events or the replay cursor (the follower has
already consumed every event it passes here).

What is shown:

- Routine activity (a tool started/finished/pending/retrying, a model step)
  becomes one *current status* derived from the run's state: the labels of
  the operations in flight (parallel operations are combined, each keyed by
  its own operation id), or "Preparing the next step." when none is in flight
  (the investigation is between tools, i.e. in a model request). Its text is
  the server's application-authored label; model-written summaries are never
  displayed.
- Rapid routine changes are coalesced: a status is rendered at once when
  nothing routine was rendered in the last ``coalesce`` seconds, otherwise
  the *latest* state is rendered when that window ends. There is one slot per
  run, never a backlog; intermediate states are simply not drawn.
- When the same status has been current for ``wait_after`` seconds, a waiting
  update states how long (at most every ``wait_every`` seconds). Nothing
  ticks while the run waits for the user, after it ended, after the follower
  detached, or while the stream is disconnected (then the status says so).
- Everything else (failures, outcome checks, steering applied or not,
  deletion proposals, the run's end and any kind this client does not know)
  is printed immediately; a terminal event first drops that run's pending
  routine status so nothing stale appears after the result.

``live`` (a terminal with the chat's line editor) keeps the status on one
redrawn line above the input. Otherwise status changes are printed as plain
lines (no control sequences), without "Preparing the next step." chatter and
with waiting updates at most every ``wait_every`` seconds.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

import click

from retail_analytics.interfaces.cli.client import JsonObject
from retail_analytics.interfaces.cli.render import (
    QUERY_ADJUSTING,
    WORKING,
    format_event,
    needs_adjustment,
    one_line,
)

DEFAULT_COALESCE_SECONDS = 1.0
DEFAULT_WAIT_AFTER_SECONDS = 10.0
DEFAULT_LIVE_WAIT_EVERY_SECONDS = 10.0
DEFAULT_PLAIN_WAIT_EVERY_SECONDS = 30.0

STARTING = "Starting the investigation."
PREPARING = "Preparing the next step."
RECONNECTING = "Reconnecting; the run's status is unavailable until it resumes."
GENERIC_TOOL = "Working on a step."

_QUERY_TOOL = "execute_analysis"
_ROUTINE = frozenset(
    {
        "run.started",
        "analysis.progress",
        "tool.started",
        "tool.succeeded",
        "tool.pending",
        "tool.retrying",
    }
)
TERMINAL = frozenset({"run.completed", "run.partial", "run.failed", "run.cancelled"})
_STATE_SUFFIX = {
    "pending": "still running",
    "retrying": "retrying after a temporary failure",
    "unknown": "confirming its outcome",
}


@dataclass
class _Operation:
    label: str
    state: str = "running"


@dataclass
class _RunView:
    acknowledged: bool = False
    started: bool = False
    following: bool = False
    waiting_for_user: bool = False
    disconnected: bool = False
    closed: bool = False
    adjusting: bool = False
    last_sequence: int = 0
    operations: dict[str, _Operation] = field(default_factory=dict)
    # The status text and when it became current (for waiting updates).
    status: str | None = None
    since: float = 0.0
    # The routine status last rendered, when, and a pending render time;
    # ``display`` is the live line (the status, perhaps with elapsed time).
    shown: str | None = None
    display: str | None = None
    shown_at: float | None = None
    due: float | None = None
    waited_at: float | None = None


class ProgressPresenter:
    """Per-run presentation state. Single-threaded; the clock is injected."""

    def __init__(
        self,
        *,
        live: bool,
        clock: Callable[[], float] = time.monotonic,
        coalesce: float = DEFAULT_COALESCE_SECONDS,
        wait_after: float = DEFAULT_WAIT_AFTER_SECONDS,
        wait_every: float | None = None,
    ) -> None:
        self._live = live
        self._clock = clock
        self._coalesce = coalesce
        self._wait_after = wait_after
        self._wait_every = wait_every or (
            DEFAULT_LIVE_WAIT_EVERY_SECONDS
            if live
            else DEFAULT_PLAIN_WAIT_EVERY_SECONDS
        )
        self._runs: dict[str, _RunView] = {}
        self.current: str | None = None
        # The live status line (styled by the caller), or None.
        self.status_line: str | None = None

    # --- lifecycle signals from the chat ---

    def acknowledge(self, run_id: str) -> None:
        """The request was acknowledged from the API receipt: its run.started
        is not shown again."""
        view = self._view(run_id)
        view.acknowledged = True

    def follow(self, run_id: str) -> None:
        """The chat follows this run (fresh or re-attached)."""
        self.current = run_id
        view = self._view(run_id)
        view.following = True
        view.disconnected = False
        if not view.closed:
            self._set_status(view, self._derive(view))
            self._render_due(view)
        self._sync()

    def stop(self, run_id: str | None) -> None:
        """No longer following (detached, stream lost, chat ending): no
        status and no ticking for it."""
        view = self._runs.get(run_id or "")
        if view is not None:
            view.following = False
            view.due = None
        self._sync()

    def resume(self, run_id: str) -> None:
        """The user answered the run's question: it continues."""
        view = self._view(run_id)
        if view.waiting_for_user and not view.closed:
            view.waiting_for_user = False
            self._set_status(view, self._derive(view))
        self._sync()

    def disconnected(self, run_id: str) -> None:
        view = self._view(run_id)
        if not view.closed:
            view.disconnected = True
            view.due = None
        self._sync()

    def connected(self, run_id: str) -> None:
        view = self._view(run_id)
        if view.disconnected and not view.closed:
            view.disconnected = False
            self._set_status(view, self._derive(view), force=True)
        self._sync()

    def close(self, run_id: str) -> None:
        """The run ended: drop its pending routine status and timers (only
        this run's). Later progress for it is never shown."""
        view = self._view(run_id)
        view.closed = True
        view.operations.clear()
        view.status = None
        view.due = None
        view.waited_at = None
        self._sync()

    # --- events ---

    def event(self, run_id: str, event: JsonObject) -> list[str]:
        """Lines to print now for one consumed event (possibly none)."""
        view = self._view(run_id)
        kind = str(event.get("kind", ""))
        sequence = event.get("sequence")
        if isinstance(sequence, int):
            if sequence <= view.last_sequence:
                return []  # already rendered by this client (replay)
            view.last_sequence = sequence
        if view.closed:
            return []
        lines: list[str] = []
        if kind in TERMINAL:
            text = format_event(event)
            self.close(run_id)
            return [text] if text else []
        if kind != "input.required":
            view.waiting_for_user = False
        tool = event.get("tool") or {}
        operation = str(
            (event.get("correlation") or {}).get("operation_id")
            or tool.get("capability")
            or "?"
        )
        if kind == "run.started":
            view.started = True
            if not view.acknowledged:
                lines.append(click.style(WORKING, dim=True))
        elif kind == "tool.started":
            label = self._label(event, tool)
            if tool.get("capability") == _QUERY_TOOL and view.adjusting:
                view.adjusting = False
                label = QUERY_ADJUSTING
            view.operations[operation] = _Operation(label)
        elif kind == "tool.succeeded":
            view.operations.pop(operation, None)
        elif kind in ("tool.pending", "tool.retrying"):
            state = "pending" if kind == "tool.pending" else "retrying"
            known = view.operations.get(operation)
            if known is None:
                view.operations[operation] = _Operation(GENERIC_TOOL, state)
            else:
                known.state = state
        elif kind == "tool.outcome_unknown":
            known = view.operations.setdefault(operation, _Operation(GENERIC_TOOL))
            known.state = "unknown"
            lines.extend(_line(format_event(event)))
        elif kind == "tool.failed":
            view.operations.pop(operation, None)
            if needs_adjustment(kind, tool):
                view.adjusting = True
            lines.extend(_line(format_event(event)))
        elif kind == "input.required":
            view.waiting_for_user = True
            view.due = None
        elif kind == "analysis.progress":
            pass  # a model step; its own text is never shown
        else:
            lines.extend(_line(format_event(event)))
        if kind in _ROUTINE or kind in ("tool.failed", "tool.outcome_unknown"):
            view.started = True
        self._set_status(view, self._derive(view))
        if kind in _ROUTINE:
            lines.extend(self._render_due(view))
        self._sync()
        return lines

    def tick(self) -> list[str]:
        """Render a coalesced status or a waiting update that is due now."""
        view = self._runs.get(self.current or "")
        if view is None or not self._active(view):
            self._sync()
            return []
        now = self._clock()
        lines: list[str] = []
        if view.due is not None and now >= view.due:
            view.due = None
            lines.extend(self._render(view, now))
        if (
            view.status is not None
            and now - view.since >= self._wait_after
            and (view.waited_at is None or now - view.waited_at >= self._wait_every)
        ):
            view.waited_at = now
            # Whole tens of seconds: honest without implying precision.
            seconds = max(int((now - view.since) // 10 * 10), int(self._wait_after))
            text = f"{view.status.rstrip('.')} ({seconds} s so far)."
            if self._live:
                view.display = text
            else:
                lines.append(click.style(f"  ... {text}", dim=True))
        self._sync()
        return lines

    def next_due(self) -> float | None:
        """Seconds until ``tick`` has something to do, or None."""
        view = self._runs.get(self.current or "")
        if view is None or not self._active(view):
            return None
        now = self._clock()
        candidates = []
        if view.due is not None:
            candidates.append(view.due - now)
        if view.status is not None:
            start = view.since + self._wait_after
            if view.waited_at is not None:
                start = max(start, view.waited_at + self._wait_every)
            candidates.append(start - now)
        return max(min(candidates), 0.0) if candidates else None

    # --- internals ---

    def _view(self, run_id: str) -> _RunView:
        view = self._runs.get(run_id)
        if view is None:
            view = self._runs[run_id] = _RunView(since=self._clock())
            if self.current is None:
                self.current = run_id
        return view

    def _active(self, view: _RunView) -> bool:
        return (
            view.following
            and not view.closed
            and not view.waiting_for_user
            and not view.disconnected
        )

    def _label(self, event: JsonObject, tool: JsonObject) -> str:
        # Application-authored labels only; anything else reads generically.
        if event.get("source", "application") != "application":
            return GENERIC_TOOL
        label = one_line(event.get("summary", ""))
        return label or GENERIC_TOOL

    def _derive(self, view: _RunView) -> str | None:
        if view.closed or view.waiting_for_user:
            return None
        if not view.operations:
            return PREPARING if view.started else STARTING
        operations = list(view.operations.values())
        if len(operations) == 1:
            only = operations[0]
            suffix = _STATE_SUFFIX.get(only.state)
            return f"{only.label.rstrip('.')}: {suffix}." if suffix else only.label
        labels = list(dict.fromkeys(o.label.rstrip(".") for o in operations))
        return f"{' / '.join(labels)} ({len(operations)} at once)."

    def _set_status(
        self, view: _RunView, status: str | None, *, force: bool = False
    ) -> None:
        if status != view.status or force:
            view.status = status
            view.since = self._clock()
            view.waited_at = None

    def _render_due(self, view: _RunView) -> list[str]:
        """Render now, or once the coalescing window ends (one slot: the
        state at that moment is what gets drawn)."""
        if not view.following or view.closed:
            return []
        now = self._clock()
        if view.shown_at is None or now - view.shown_at >= self._coalesce:
            view.due = None
            return self._render(view, now)
        if view.due is None:
            view.due = view.shown_at + self._coalesce
        return []

    def _render(self, view: _RunView, now: float) -> list[str]:
        status = view.status
        if status is None or status == view.shown:
            return []
        view.shown = view.display = status
        if status == STARTING or (not self._live and status == PREPARING):
            # A placeholder, or (plain output) between-step chatter that is
            # not printed: the next real activity is not held back by it.
            return []
        view.shown_at = now
        if self._live:
            return []
        return [click.style(f"  > {status}", fg="cyan")]

    def _sync(self) -> None:
        if not self._live:
            self.status_line = None
            return
        view = self._runs.get(self.current or "")
        if view is None or view.closed or view.waiting_for_user or not view.following:
            self.status_line = None
        elif view.disconnected:
            self.status_line = RECONNECTING
        else:
            self.status_line = view.display


def _line(text: str | None) -> list[str]:
    return [text] if text else []
