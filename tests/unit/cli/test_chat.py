from __future__ import annotations

from typing import Any

import httpx
from click.testing import CliRunner

from retail_analytics.interfaces.cli.app import cli
from tests.unit.cli.fake_backend import (
    Backend,
    error,
    event,
    run_view,
    sse,
)
from tests.unit.cli.test_commands import PREVIEW, deletion_backend

PROPOSAL = "a" * 32


def chat(backend: Backend, lines: str, *args: str) -> Any:
    return CliRunner().invoke(cli, ["chat", *args], obj=backend.client, input=lines)


def base() -> Backend:
    backend = Backend()
    backend.overrides[("POST", "/v1/sessions")] = lambda r: httpx.Response(
        201,
        json={
            "session_id": "s1",
            "created_at": "2026-10-09T10:00:00Z",
            "last_activity_at": "2026-10-09T10:00:00Z",
        },
    )
    backend.overrides[("POST", "/v1/sessions/s1/messages")] = lambda r: httpx.Response(
        202, json={"input_id": "i1", "kind": "request", "run_id": "r1"}
    )
    return backend


def finishing_stream(answer: str = "Revenue was 10.") -> Any:
    return lambda r: httpx.Response(
        200,
        content=sse(
            [
                event(1, "run.started", "Started."),
                event(
                    2,
                    "tool.started",
                    "Running a query.",
                    tool={
                        "capability": "execute_analysis",
                        "capability_version": 1,
                        "attempt": 1,
                        "error_code": None,
                    },
                ),
                event(3, "run.completed", "Done."),
            ],
            end="completed",
        ),
    )


def test_ask_follow_up_and_survive_bad_commands() -> None:
    backend = base()
    backend.runs["r1"] = run_view("completed", answer="**Revenue** was 10.")
    backend.streams = [finishing_stream(), finishing_stream()]
    lines = "\n".join(
        [
            "How much revenue?",
            "/nonsense",
            "/report",
            "/queue",
            "/export",
            "/confirm",
            "/search",
            "and by state?",
            "/quit",
        ]
    )
    result = chat(backend, lines + "\n")
    assert result.exit_code == 0, result.output
    assert result.output.count("Revenue was 10.") == 2
    assert "Running a query." in result.output
    assert "Unknown command /nonsense" in result.output
    assert "Usage: /report" in result.output
    assert "Usage: /confirm" in result.output
    keys = [
        backend.json_body(r)["submission_key"]
        for r in backend.calls("POST", "/messages")
    ]
    assert len(keys) == 2 and keys[0] != keys[1]


def test_clarification_is_answered_from_the_next_line() -> None:
    backend = base()
    backend.runs["r1"] = run_view(
        "waiting_for_input", answer=None, question=("q1", "Which period?")
    )

    def answered(request: httpx.Request) -> httpx.Response:
        backend.runs["r1"] = run_view("completed", answer="Last month: 10.")
        return httpx.Response(
            202, json={"input_id": "i", "kind": "answer", "run_id": "r1"}
        )

    backend.overrides[("POST", "/v1/runs/r1/answers")] = answered
    ask = event(
        2,
        "input.required",
        "Waiting.",
        input_request={"question_id": "q1", "question": "Which period?"},
    )
    backend.streams = [
        lambda r: httpx.Response(
            200, content=sse([event(1, "run.started", "Go."), ask])
        ),
    ]
    # The stream stays open in reality; here it closes, so the follower
    # reconnects after the question and receives the rest.
    backend.streams.append(
        lambda r: httpx.Response(
            200, content=sse([event(3, "run.completed", "Done.")], end="completed")
        )
    )
    result = chat(backend, "sales?\nlast month\n/quit\n")
    assert result.exit_code == 0, result.output
    assert "Which period?" in result.output
    assert "Last month: 10." in result.output
    answers = backend.calls("POST", "/answers")
    assert backend.json_body(answers[0])["text"] == "last month"


def test_backend_errors_never_end_the_session() -> None:
    backend = base()
    backend.overrides[("POST", "/v1/sessions/s1/messages")] = lambda r: error(
        409, "active_run_exists", "busy", {"active_run_id": "r9"}
    )
    backend.overrides[("GET", "/v1/reports")] = lambda r: error(403, "forbidden", "no")
    result = chat(backend, "hello\n/reports\n/help\n")
    assert result.exit_code == 0
    assert "error [active_run_exists]" in result.output
    assert "error [forbidden]" in result.output
    assert "/queue <text>" in result.output


def test_proposed_deletion_is_shown_from_the_server_and_needs_typed_confirmation() -> (
    None
):
    for typed, deleted in (("delete 2 reports", True), ("no", False)):
        backend = deletion_backend()
        backend.overrides[("POST", "/v1/sessions")] = base().overrides[
            ("POST", "/v1/sessions")
        ]
        backend.overrides[("POST", "/v1/sessions/s1/messages")] = base().overrides[
            ("POST", "/v1/sessions/s1/messages")
        ]
        backend.overrides[("GET", f"/v1/deletion-proposals/{PROPOSAL}")] = lambda r: (
            httpx.Response(200, json={**PREVIEW, "proposal_id": PROPOSAL})
        )
        backend.overrides[("POST", f"/v1/deletion-proposals/{PROPOSAL}/confirm")] = (
            lambda r: httpx.Response(
                200,
                json={
                    "proposal_id": PROPOSAL,
                    "report_ids": ["rep1", "rep2"],
                    "deleted_at": "2026-10-09T10:05:00Z",
                    "recoverable_until": "2026-10-16T10:05:00Z",
                },
            )
        )
        backend.runs["r1"] = run_view(
            "completed",
            answer=f"I prepared proposal {PROPOSAL}. Please confirm in the app.",
        )
        backend.streams = [finishing_stream()]
        result = chat(
            backend,
            f"delete my client x reports\n/confirm {PROPOSAL}\n{typed}\n/quit\n",
        )
        assert "Client X" in result.output and "(title hidden)" in result.output
        assert "Nothing is deleted yet" in result.output
        sent = backend.calls("POST", "/confirm")
        assert bool(sent) is deleted, result.output
        if deleted:
            assert "Deleted 2 report(s)" in result.output
        else:
            assert "Nothing was deleted." in result.output


def test_reopening_a_session_restores_the_pending_question() -> None:
    backend = Backend()
    backend.overrides[("GET", "/v1/sessions/s1")] = lambda r: httpx.Response(
        200,
        json={
            "session_id": "s1",
            "created_at": "x",
            "last_activity_at": "x",
            "runs": [
                {
                    "run_id": "r1",
                    "session_id": "s1",
                    "status": "waiting_for_input",
                    "active": True,
                    "created_at": "x",
                    "updated_at": "x",
                    "completed_at": None,
                }
            ],
        },
    )
    backend.runs["r1"] = run_view(
        "waiting_for_input", answer=None, question=("q1", "Which period?")
    )
    ask = event(
        2,
        "input.required",
        "Waiting.",
        input_request={"question_id": "q1", "question": "Which period?"},
    )
    backend.streams = [
        lambda r: httpx.Response(
            200, content=sse([event(1, "run.started", "Go."), ask])
        )
    ]
    result = chat(backend, "", "--session", "s1")
    assert "Resuming run r1" in result.output
    assert "Which period?" in result.output
    assert "still active and was not cancelled" in result.output
    assert "waiting for your answer" in result.output
    assert backend.calls("POST", "/cancel") == []


def test_cancel_in_chat_reports_cancelling_honestly() -> None:
    backend = base()
    backend.runs["r1"] = run_view(
        "waiting_for_input", answer=None, question=("q1", "Which period?")
    )
    ask = event(
        2,
        "input.required",
        "Waiting.",
        input_request={"question_id": "q1", "question": "Which period?"},
    )
    backend.streams = [
        lambda r: httpx.Response(
            200, content=sse([event(1, "run.started", "Go."), ask])
        ),
        lambda r: httpx.Response(
            200,
            content=sse([event(3, "run.cancelled", "Stopped.")], end="cancelled"),
        ),
    ]

    def cancel(request: httpx.Request) -> httpx.Response:
        backend.runs["r1"] = run_view("cancelled", answer=None)
        return httpx.Response(202, json={"run": run_view("cancelling", answer=None)})

    backend.overrides[("POST", "/v1/runs/r1/cancel")] = cancel
    result = chat(backend, "sales?\n/cancel\n/quit\n")
    assert result.exit_code == 0, result.output
    assert "Cancellation requested: no new work will start" in result.output
    assert "in-flight external work" in result.output
    assert "The run was cancelled" in result.output
