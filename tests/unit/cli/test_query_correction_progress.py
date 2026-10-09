"""A rejected query attempt reads as ongoing correction, never as a failed
investigation; real failures keep their truthful status."""

from __future__ import annotations

from typing import Any

import click
import httpx
import pytest
from click.testing import CliRunner

from retail_analytics.interfaces.cli.app import cli
from retail_analytics.interfaces.cli.render import (
    QUERY_ADJUSTING,
    QUERY_NEEDS_ADJUSTMENT,
    EventFormatter,
    format_event,
)
from tests.unit.cli.fake_backend import Backend, event, run_view, sse

QUERY = "execute_analysis"


def tool(seq: int, kind: str, summary: str, code: str | None = None) -> Any:
    return event(
        seq,
        kind,
        summary,
        tool={
            "capability": QUERY,
            "capability_version": 1,
            "attempt": 1,
            "error_code": code,
        },
        correlation={"run_id": "r1", "operation_id": f"op{seq}"},
    )


REJECTED = tool(
    2,
    "tool.failed",
    "Joins to CTEs or subqueries are not supported",
    "UNSUPPORTED_SQL",
)


def lines(*events: Any) -> list[str]:
    formatter = EventFormatter()
    return [click.unstyle(line) for e in events if (line := formatter(e))]


def test_rejection_then_correction_reads_as_ongoing_progress() -> None:
    shown = lines(
        event(0, "run.started", "Started."),
        tool(1, "tool.started", "Running a query."),
        REJECTED,
        tool(3, "tool.started", "Running a query."),
        tool(4, "tool.succeeded", "Completed."),
        event(5, "run.completed", "Done."),
    )
    assert shown == [
        "Working on it.",
        "  > Running a query.",
        f"  ~ {QUERY_NEEDS_ADJUSTMENT}",
        f"  > {QUERY_ADJUSTING}",
        "  ok Completed.",
    ]
    text = "\n".join(shown)
    assert "UNSUPPORTED_SQL" not in text and "not supported" not in text


def test_no_adjustment_is_announced_unless_a_new_query_starts() -> None:
    # The assistant may ask, answer or stop instead of reformulating.
    shown = lines(
        REJECTED,
        event(3, "input.required", "Waiting for your answer."),
        event(4, "run.failed", "The investigation could not finish."),
    )
    assert QUERY_ADJUSTING not in "\n".join(shown)
    assert shown[-1] == "The run failed: The investigation could not finish."
    # A new run never inherits a pending adjustment.
    formatter = EventFormatter()
    formatter(REJECTED)
    formatter(event(5, "run.completed", "Done."))
    started = formatter(tool(6, "tool.started", "Running a query."))
    assert started is not None and QUERY_ADJUSTING not in started


@pytest.mark.parametrize(
    "code", ["ACCESS_DENIED", "BUDGET_EXCEEDED", "INTERNAL_ERROR", "TEMPORARY_FAILURE"]
)
def test_access_budget_and_real_failures_keep_their_status(code: str) -> None:
    failed = tool(2, "tool.failed", "This data is not available to you.", code)
    shown = lines(failed, tool(3, "tool.started", "Running a query."))
    assert shown[0] == f"  x This data is not available to you. [{code}]"
    assert QUERY_NEEDS_ADJUSTMENT not in shown[0]
    assert shown[1] == "  > Running a query."


def test_other_tools_failures_are_not_relabelled() -> None:
    other = event(
        2,
        "tool.failed",
        "Bad input.",
        tool={
            "capability": "save_report",
            "capability_version": 1,
            "attempt": 1,
            "error_code": "INVALID_INPUT",
        },
    )
    assert click.unstyle(format_event(other) or "") == "  x Bad input. [INVALID_INPUT]"


def test_follow_shows_correction_and_ends_with_the_normal_answer() -> None:
    backend = Backend()
    backend.runs["r1"] = run_view("completed", answer="September revenue was 10.")
    backend.streams = [
        lambda r: httpx.Response(
            200,
            content=sse(
                [
                    event(1, "run.started", "Started."),
                    tool(2, "tool.started", "Running a query."),
                    tool(3, "tool.failed", "Joins are limited.", "UNSUPPORTED_SQL"),
                    tool(4, "tool.started", "Running a query."),
                    tool(5, "tool.succeeded", "Completed."),
                    event(6, "run.completed", "Done."),
                ],
                end="completed",
            ),
        )
    ]
    result = CliRunner().invoke(cli, ["follow", "r1"], obj=backend.client)
    assert result.exit_code == 0, result.output
    assert QUERY_NEEDS_ADJUSTMENT in result.output
    assert QUERY_ADJUSTING in result.output
    assert "September revenue was 10." in result.output
    assert "UNSUPPORTED_SQL" not in result.output
    assert "failed" not in result.output.lower()
