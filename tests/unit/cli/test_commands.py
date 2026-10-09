from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
from click.testing import CliRunner

from retail_analytics.interfaces.cli.app import cli
from tests.unit.cli.fake_backend import (
    TOKEN,
    Backend,
    error,
    event,
    run_view,
    sse,
)


def invoke(backend: Backend, *args: str, input: str | None = None) -> Any:
    return CliRunner().invoke(cli, list(args), obj=backend.client, input=input)


def script_clarification(backend: Backend) -> None:
    backend.overrides[("POST", "/v1/sessions")] = lambda r: httpx.Response(
        201,
        json={
            "session_id": "s1",
            "created_at": "2026-10-09T10:00:00Z",
            "last_activity_at": "2026-10-09T10:00:00Z",
        },
    )
    backend.overrides[("POST", "/v1/sessions/s1/runs")] = lambda r: httpx.Response(
        202, json={"run_id": "r1", "status": "running", "created": True}
    )
    backend.runs["r1"] = run_view(
        "waiting_for_input",
        answer=None,
        question=("q1", "Which sales period should I use?"),
    )

    def answered(request: httpx.Request) -> httpx.Response:
        assert backend.json_body(request)["question_id"] == "q1"
        backend.runs["r1"] = run_view("completed", answer="Last month: 10.")
        return httpx.Response(
            202, json={"input_id": "i", "kind": "answer", "run_id": "r1"}
        )

    backend.overrides[("POST", "/v1/runs/r1/answers")] = answered
    ask_event = event(
        2,
        "input.required",
        "Waiting for your answer.",
        input_request={
            "question_id": "q1",
            "question": "Which sales period should I use?",
        },
    )
    backend.streams = [
        lambda r: httpx.Response(
            200, content=sse([event(1, "run.started", "Started."), ask_event])
        ),
        lambda r: httpx.Response(
            200,
            content=sse([event(3, "run.completed", "Done.")], end="completed"),
        ),
    ]


def test_ask_answers_a_clarification_and_prints_the_answer() -> None:
    backend = Backend()
    script_clarification(backend)
    result = invoke(backend, "ask", "sales?", "--answer", "last month")
    assert result.exit_code == 0, result.output
    assert "Which sales period should I use?" in result.output
    assert "Last month: 10." in result.output
    assert TOKEN not in result.output
    # The second connection resumed after the question event.
    assert backend.calls("GET", "/events")[1].headers["Last-Event-ID"] == "ev2"


def test_ask_without_an_answer_exits_waiting_and_names_the_next_command() -> None:
    backend = Backend()
    script_clarification(backend)
    result = invoke(backend, "ask", "sales?")
    assert result.exit_code == 4
    assert "analytics answer r1" in result.output


def test_json_mode_prints_one_machine_readable_object() -> None:
    backend = Backend()
    script_clarification(backend)
    result = invoke(backend, "ask", "sales?", "--answer", "x", "--json")
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["run"]["status"] == "completed"


def test_same_submission_key_is_used_for_session_and_run_so_rerun_is_safe() -> None:
    backend = Backend()
    script_clarification(backend)
    invoke(backend, "ask", "sales?", "--answer", "x", "--submission-key", "k9")
    body = backend.json_body(backend.calls("POST", "/s1/runs")[0])
    assert body == {"text": "sales?", "submission_key": "k9"}
    session = backend.json_body(backend.calls("POST", "/v1/sessions")[0])
    assert session == {"submission_key": "k9-session"}


def test_partial_answers_are_loudly_marked_and_exit_code_differs() -> None:
    backend = Backend()
    backend.runs["r1"] = run_view(
        "partial",
        answer="## Disclosures\n- Result was truncated at 1000 rows.\n"
        "## Next steps\n- Narrow the period.",
    )
    backend.streams = [
        lambda r: httpx.Response(
            200, content=sse([event(1, "run.partial", "partial")], end="partial")
        )
    ]
    result = invoke(backend, "follow", "r1")
    assert result.exit_code == 3
    assert "PARTIAL RESULT" in result.output
    assert "== DISCLOSURES ==" in result.output
    assert "== NEXT STEPS ==" in result.output
    assert "[ ] Narrow the period." in result.output


def test_withheld_answer_and_terminal_escape_sequences() -> None:
    backend = Backend()
    backend.runs["r1"] = run_view(
        "completed", answer="Blocked \x1b[31mred\x1b[0m\x07 text", withheld=True
    )
    backend.streams = [
        lambda r: httpx.Response(
            200,
            content=sse(
                [event(1, "analysis.progress", "step \x1b]0;evil\x07 ok")],
                end="completed",
            ),
        )
    ]
    result = invoke(backend, "follow", "r1")
    assert "withheld by the privacy check" in result.output
    assert "\x1b" not in result.output and "\x07" not in result.output
    assert "evil" not in result.output


def test_errors_use_one_format_and_exit_one() -> None:
    backend = Backend()
    backend.overrides[("GET", "/v1/runs/nope")] = lambda r: error(
        404, "not_found", "No such run."
    )
    result = invoke(backend, "show", "nope")
    assert result.exit_code == 1
    assert "error [not_found]: No such run." in result.output

    unauth = CliRunner().invoke(
        cli,
        ["sessions"],
        obj=lambda: Backend().client(token="bad-token-zzz"),  # noqa: S106
    )
    assert unauth.exit_code == 1
    assert "error [unauthenticated]" in unauth.output
    assert "bad-token-zzz" not in unauth.output

    anonymous = CliRunner().invoke(
        cli, ["sessions"], obj=lambda: Backend().client(token=None)
    )
    assert "CLI_TOKEN" in anonymous.output and anonymous.exit_code == 1


def test_unreachable_backend_is_reported_not_raised() -> None:
    backend = Backend()

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    backend.overrides[("GET", "/v1/sessions")] = refuse
    result = invoke(backend, "sessions")
    assert result.exit_code == 1
    assert "error [unreachable]" in result.output


def test_active_run_conflict_points_at_the_active_run() -> None:
    backend = Backend()
    script_clarification(backend)
    backend.overrides[("POST", "/v1/sessions/s1/runs")] = lambda r: error(
        409, "active_run_exists", "busy", {"active_run_id": "r7"}
    )
    result = invoke(backend, "ask", "again", "--session", "s1")
    assert result.exit_code == 1
    assert "analytics follow r7" in result.output


def test_cancel_reports_pending_external_cancellation_truthfully() -> None:
    backend = Backend()
    backend.overrides[("POST", "/v1/runs/r1/cancel")] = lambda r: httpx.Response(
        202, json={"run": {**run_view("cancelling", answer=None)}}
    )
    result = invoke(backend, "cancel", "r1")
    assert result.exit_code == 7
    assert "not yet known to be fully stopped" in result.output

    backend.overrides[("POST", "/v1/runs/r1/cancel")] = lambda r: httpx.Response(
        202, json={"run": {**run_view("cancelled", answer=None)}}
    )
    done = invoke(backend, "cancel", "r1")
    assert done.exit_code == 6 and "Cancelled." in done.output


REPORT = {
    "report_id": "rep1",
    "version": 2,
    "title": "Quarterly \x1b[1mrevenue",
    "created_at": "2026-10-01T00:00:00Z",
    "session_id": "s1",
    "run_id": "r1",
    "markdown": "# Revenue\n\n**Total** was `10`.",
    "evidence": [
        {
            "evidence_id": "ev1",
            "kind": "query",
            "computed_at": "2026-10-01T00:00:00Z",
            "period_start": "2026-07-01",
            "period_end": "2026-10-01",
            "truncated": True,
            "columns": ["state", "revenue"],
            "rows": [["CA", "5"], ["NY", "5"]],
        }
    ],
}


def test_reports_list_search_show_and_export(tmp_path: Path) -> None:
    backend = Backend()
    listing = {
        "report_id": "rep1",
        "version": 2,
        "title": "Quarterly revenue",
        "created_at": "2026-10-01T00:00:00Z",
        "session_id": "s1",
        "evidence_count": 1,
        "access": "available",
    }
    hidden = {**listing, "report_id": "rep2", "title": None, "access": "access_changed"}
    backend.overrides[("GET", "/v1/reports")] = lambda r: httpx.Response(
        200, json={"reports": [listing, hidden]}
    )
    backend.overrides[("GET", "/v1/reports/search")] = lambda r: httpx.Response(
        200,
        json={
            "matches": [
                {"report": listing, "matched_in": "content", "snippet": "…10…"}
            ],
            "scanned": 2,
            "withheld": 1,
            "scan_limited": True,
        },
    )
    backend.overrides[("GET", "/v1/reports/rep1")] = lambda r: httpx.Response(
        200, json=REPORT
    )
    backend.overrides[("GET", "/v1/reports/rep1/export")] = lambda r: httpx.Response(
        200,
        content=b"# Revenue\n",
        headers={
            "content-type": "text/markdown",
            "content-disposition": 'attachment; filename="rep1.md"',
        },
    )
    listed = invoke(backend, "reports", "list")
    assert "rep1" in listed.output and "your product access changed" in listed.output
    found = invoke(backend, "reports", "search", "revenue")
    assert "matched in content" in found.output
    assert "not searched because your product access changed" in found.output
    shown = invoke(backend, "reports", "show", "rep1")
    assert shown.exit_code == 0
    assert "TRUNCATED" in shown.output and "\x1b" not in shown.output
    assert "REVENUE" in shown.output and "state" in shown.output
    out = tmp_path / "r.md"
    exported = invoke(backend, "reports", "export", "rep1", "-o", str(out))
    assert exported.exit_code == 0 and out.read_bytes() == b"# Revenue\n"


PREVIEW = {
    "proposal_id": "p1",
    "status": "pending",
    "expires_at": "2026-10-09T10:10:00Z",
    "count": 2,
    "items": [
        {
            "report_id": "rep1",
            "version": 2,
            "title": "Client X",
            "created_at": "2026-10-01T00:00:00Z",
        },
        {
            "report_id": "rep2",
            "version": 1,
            "title": None,
            "created_at": "2026-10-02T00:00:00Z",
        },
    ],
}


def deletion_backend() -> Backend:
    backend = Backend()
    backend.overrides[("GET", "/v1/deletion-proposals/p1")] = lambda r: httpx.Response(
        200, json=PREVIEW
    )
    backend.overrides[("POST", "/v1/deletion-proposals/p1/confirm")] = lambda r: (
        httpx.Response(
            200,
            json={
                "proposal_id": "p1",
                "report_ids": ["rep1", "rep2"],
                "deleted_at": "2026-10-09T10:05:00Z",
                "recoverable_until": "2026-10-16T10:05:00Z",
            },
        )
    )
    return backend


def test_deletion_needs_the_exact_typed_phrase_and_shows_every_report() -> None:
    backend = deletion_backend()
    for typed in (
        "\n",
        "yes\n",
        "y\n",
        "delete 1 report\n",
        "delete 2 reports please\n",
    ):
        result = invoke(backend, "deletion", "confirm", "p1", input=typed)
        assert result.exit_code == 1, typed
        assert "rep1" in result.output and "rep2" in result.output
        assert "(title hidden)" in result.output
        assert "Nothing was deleted." in result.output
    assert backend.calls("POST", "/confirm") == []

    ok = invoke(backend, "deletion", "confirm", "p1", input="delete 2 reports\n")
    assert ok.exit_code == 0, ok.output
    assert "Deleted 2 report(s)" in ok.output
    sent = backend.calls("POST", "/confirm")
    assert len(sent) == 1 and backend.json_body(sent[0]) == {"confirm": True}


def test_deletion_has_no_blanket_yes_flag() -> None:
    result = invoke(deletion_backend(), "deletion", "confirm", "p1", "--yes")
    assert result.exit_code == 2


def test_deletion_confirm_lost_response_is_reported_as_unknown() -> None:
    backend = deletion_backend()

    def lost(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("lost", request=request)

    backend.overrides[("POST", "/v1/deletion-proposals/p1/confirm")] = lost
    result = invoke(
        backend, "deletion", "confirm", "p1", "--confirm-text", "delete 2 reports"
    )
    assert "not known whether" in result.output
    assert len(backend.calls("POST", "/confirm")) == 1


def test_expired_proposal_error_is_clear() -> None:
    backend = deletion_backend()
    backend.overrides[("GET", "/v1/deletion-proposals/p1")] = lambda r: error(
        410, "expired", "This deletion proposal expired."
    )
    result = invoke(backend, "deletion", "show", "p1")
    assert result.exit_code == 1 and "expired" in result.output


NOTICE = {
    "kind": "definition_changed",
    "message": (
        "The definitions recorded for this report's evidence include "
        '"revenue" as completed item sales (version 1). '
        'Your current definition of "revenue" is shipped item sales (version 1). '
        "The figures in this report have not been recalculated; using your "
        "current definition requires recalculating them. The recorded "
        "definition is context from the fields the queries read; it does not "
        "confirm that the queries calculated it."
    ),
    "subject": "revenue",
    "report_definition": "completed item sales (version 1)",
    "current_definition": "shipped item sales (version 1)",
    "recalculation_required": True,
}


def test_reports_show_and_export_render_definition_notices(tmp_path: Path) -> None:
    backend = Backend()
    backend.overrides[("GET", "/v1/reports/rep1")] = lambda r: httpx.Response(
        200, json={**REPORT, "definition_notices": [NOTICE]}
    )
    backend.overrides[("GET", "/v1/reports/rep1/export")] = lambda r: httpx.Response(
        200,
        content=b"# Revenue\n",
        headers={
            "content-type": "text/markdown",
            "content-disposition": 'attachment; filename="rep1.md"',
            "x-report-definition-notices": json.dumps([NOTICE]),
        },
    )
    shown = invoke(backend, "reports", "show", "rep1")
    assert shown.exit_code == 0
    assert f"DEFINITIONS: {NOTICE['message']}" in shown.output
    # The notice is shown before the saved text, which itself is unchanged.
    assert shown.output.index("DEFINITIONS:") < shown.output.index("REVENUE")

    out = tmp_path / "r.md"
    exported = invoke(backend, "reports", "export", "rep1", "-o", str(out))
    assert exported.exit_code == 0
    assert out.read_bytes() == b"# Revenue\n"
    assert "have not been recalculated" in exported.output
