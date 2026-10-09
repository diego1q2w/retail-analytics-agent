"""``analytics`` command group. Talks to the backend only through HTTP.

Bootstrap passes a client factory in ``ctx.obj`` (already carrying the bearer
token in its headers) so commands never build their own transport, read
configuration or see the token.

Exit codes of ``ask``/``answer``/``follow``/``cancel``: 0 completed, 1 error,
2 usage, 3 partial result, 4 waiting for your answer, 5 failed, 6 cancelled,
7 cancellation still being confirmed.
"""

from __future__ import annotations

import contextlib
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click
import httpx

from retail_analytics.interfaces.cli.chat import Chat
from retail_analytics.interfaces.cli.client import (
    ApiClient,
    ApiError,
    JsonObject,
    Unreachable,
    new_submission_key,
)
from retail_analytics.interfaces.cli.deletion import confirm_deletion
from retail_analytics.interfaces.cli.follow import DEFAULT_STALL_SECONDS, StreamLost
from retail_analytics.interfaces.cli.render import (
    confirmation_phrase,
    format_deletion_preview,
    format_error,
    format_question,
    format_report,
    format_report_list,
    format_report_search,
    format_run_line,
    format_run_result,
    format_sessions,
    format_versions,
    one_line,
)
from retail_analytics.interfaces.cli.runs import Driven, Question, drive_run

ClientFactory = Callable[[], httpx.Client]

EXIT_ERROR = 1
EXIT_PARTIAL = 3
EXIT_WAITING = 4
EXIT_FAILED = 5
EXIT_CANCELLED = 6
EXIT_CANCELLING = 7
_STATUS_EXIT = {
    "completed": 0,
    "partial": EXIT_PARTIAL,
    "failed": EXIT_FAILED,
    "cancelled": EXIT_CANCELLED,
    "cancelling": EXIT_CANCELLING,
}


def _out(text: str) -> None:
    click.echo(text)


def _api(ctx: click.Context) -> ApiClient:
    factory: ClientFactory = ctx.obj
    return ApiClient(factory())


def _fail(error: ApiError | Unreachable | StreamLost) -> SystemExit:
    if isinstance(error, StreamLost):
        click.echo(
            "error [stream_lost]: the event stream could not be re-established; the "
            "run keeps working. Resume with: analytics follow <run id> --after "
            f"{error.last_event_id or '<none>'}",
            err=True,
        )
    else:
        click.echo(format_error(error), err=True)
    return SystemExit(EXIT_ERROR)


def guarded(func: Callable[..., None]) -> Callable[..., None]:
    """Turn API/transport failures into one consistent error line + exit 1."""

    @click.pass_context
    def wrapper(ctx: click.Context, *args: Any, **kwargs: Any) -> None:
        try:
            func(_api(ctx), *args, **kwargs)
        except (ApiError, Unreachable, StreamLost) as error:
            raise _fail(error) from None

    wrapper.__name__ = func.__name__
    wrapper.__doc__ = func.__doc__
    return wrapper


def _emit(data: Any) -> None:
    click.echo(json.dumps(data, indent=2, sort_keys=True, default=str))


json_option = click.option("--json", "as_json", is_flag=True, help="Print JSON.")


@click.group()
@click.version_option(package_name="retail-analytics-agent", prog_name="analytics")
def cli() -> None:
    """Retail analytics assistant.

    Set ANALYTICS_CLI_TOKEN (or ANALYTICS_CLI_TOKEN_FILE) to your access token and
    ANALYTICS_CLI_API_URL to the backend. Start with: analytics chat
    """


@cli.command()
@click.pass_obj
def status(make_client: ClientFactory) -> None:
    """Check that the backend is reachable and show its mode."""
    try:
        with make_client() as client:
            response = client.get("/healthz")
            response.raise_for_status()
            body = response.json()
    except httpx.HTTPError as exc:
        raise click.ClickException(
            f"backend unavailable ({type(exc).__name__})"
        ) from None
    click.echo(
        f"backend {body['status']} (mode={body['mode']}, version={body['version']})"
    )


# --- chat ---


@cli.command()
@click.option("--session", "session_id", help="Reopen this session.")
@click.option("--resume", is_flag=True, help="Reopen your most recent session.")
@click.option("--stall-seconds", default=DEFAULT_STALL_SECONDS, hidden=True)
@guarded
def chat(
    api: ApiClient, session_id: str | None, resume: bool, stall_seconds: float
) -> None:
    """Interactive chat. Type /help inside.

    Closing the chat or pressing Ctrl-C while a run works only detaches: the
    run keeps going. /cancel cancels. Reopen with --session or --resume to see
    pending questions and progress again.
    """
    reopen = session_id is not None or resume
    if session_id is None and resume:
        sessions = api.list_sessions(limit=1)["sessions"]
        if sessions:
            session_id = str(sessions[0]["session_id"])
        else:
            reopen = False
    if session_id is None:
        session_id = str(api.open_session(new_submission_key())["session_id"])
    interactive = sys.stdin.isatty()
    if interactive:
        with contextlib.suppress(ImportError):
            import readline  # noqa: F401  (line editing for input)
    _out(f"Session {session_id}. Ask a question; /help lists commands, /quit leaves.")
    Chat(
        api,
        session_id,
        out=_out,
        stdin=sys.stdin,
        interactive=interactive,
        stall_seconds=stall_seconds,
    ).run(resume=reopen)


# --- scriptable run commands ---


def _finish(driven: Driven, as_json: bool) -> None:
    if as_json:
        _emit(
            {
                "run": driven.run,
                "waiting_for": None
                if driven.question is None
                else {
                    "question_id": driven.question.question_id,
                    "question": driven.question.text,
                },
            }
        )
    if driven.state == "waiting":
        click.echo(
            f"Waiting for your answer. Reply with: analytics answer "
            f'{driven.run["run_id"]} "<text>"',
            err=True,
        )
        raise SystemExit(EXIT_WAITING)
    raise SystemExit(_STATUS_EXIT.get(str(driven.run.get("status")), EXIT_ERROR))


def _asker(answer_prompt: bool) -> Callable[[Question], str | None] | None:
    if answer_prompt and sys.stdin.isatty():
        return lambda _q: click.prompt("answer", default="", show_default=False) or None
    return None


def _drive(
    api: ApiClient,
    run_id: str,
    *,
    answers: tuple[str, ...],
    key: str | None,
    after: str | None,
    as_json: bool,
    wait: bool = True,
) -> None:
    if not wait:
        return
    driven = drive_run(
        api,
        run_id,
        out=lambda t: click.echo(t, err=as_json),
        after=after,
        answers=answers,
        ask=_asker(True),
        key=key,
        quiet=False,
    )
    _finish(driven, as_json)


@cli.command()
@click.argument("text")
@click.option("--session", "session_id", help="Ask inside this existing session.")
@click.option(
    "--answer", "answers", multiple=True, help="Answer to a clarification, in order."
)
@click.option(
    "--submission-key",
    help="Stable key: re-running the same command never starts a second run.",
)
@click.option("--no-wait", is_flag=True, help="Start the run and print its ID.")
@json_option
@guarded
def ask(
    api: ApiClient,
    text: str,
    session_id: str | None,
    answers: tuple[str, ...],
    submission_key: str | None,
    no_wait: bool,
    as_json: bool,
) -> None:
    """Ask one question and follow it to the end (scriptable)."""
    key = submission_key or new_submission_key()
    if session_id is None:
        session_id = str(api.open_session(f"{key}-session")["session_id"])
    handle = api.start_run(session_id, text, key)
    run_id = str(handle["run_id"])
    if no_wait:
        click.echo(
            json.dumps(handle) if as_json else f"run {run_id} in session {session_id}"
        )
        return
    if not as_json:
        click.echo(f"session {session_id}  run {run_id}")
    _drive(api, run_id, answers=answers, key=key, after=None, as_json=as_json)


@cli.command()
@click.argument("run_id")
@click.argument("text")
@click.option(
    "--question-id", help="The question being answered (default: the open one)."
)
@click.option("--submission-key")
@click.option("--no-wait", is_flag=True)
@json_option
@guarded
def answer(
    api: ApiClient,
    run_id: str,
    text: str,
    question_id: str | None,
    submission_key: str | None,
    no_wait: bool,
    as_json: bool,
) -> None:
    """Answer a run's open clarification question."""
    key = submission_key or new_submission_key()
    if question_id is None:
        run = api.get_run(run_id)
        question = run.get("question")
        if not isinstance(question, dict):
            raise ApiError("no_open_question", "that run is not waiting for an answer")
        question_id = str(question["question_id"])
    api.answer(run_id, question_id, text, key)
    _drive(
        api, run_id, answers=(), key=key, after=None, as_json=as_json, wait=not no_wait
    )


@cli.command()
@click.argument("run_id")
@click.option("--after", help="Resume after this event ID (Last-Event-ID).")
@click.option("--answer", "answers", multiple=True)
@click.option("--stall-seconds", default=DEFAULT_STALL_SECONDS, hidden=True)
@json_option
@guarded
def follow(
    api: ApiClient,
    run_id: str,
    after: str | None,
    answers: tuple[str, ...],
    stall_seconds: float,
    as_json: bool,
) -> None:
    """Attach to a run (from any process) and show its progress and result."""
    driven = drive_run(
        api,
        run_id,
        out=lambda t: click.echo(t, err=as_json),
        after=after,
        answers=answers,
        ask=_asker(True),
        stall_seconds=stall_seconds,
    )
    _finish(driven, as_json)


@cli.command()
@click.argument("run_id")
@click.argument("text")
@click.option("--submission-key")
@guarded
def steer(api: ApiClient, run_id: str, text: str, submission_key: str | None) -> None:
    """Refine the active run with more instructions."""
    api.steer(run_id, text, submission_key or new_submission_key())
    click.echo("Steering sent.")


@cli.command()
@click.argument("session_id")
@click.argument("text")
@click.option("--submission-key")
@guarded
def queue(
    api: ApiClient, session_id: str, text: str, submission_key: str | None
) -> None:
    """Queue a separate question behind the session's active run."""
    receipt = api.send_message(
        session_id, text, submission_key or new_submission_key(), mode="queue"
    )
    if receipt.get("run_id"):
        click.echo(f"Started now as run {receipt['run_id']}.")
    else:
        click.echo("Queued: it starts when the active run finishes.")


@cli.command()
@click.argument("run_id")
@click.option("--wait", is_flag=True, help="Follow until cancellation has settled.")
@guarded
def cancel(api: ApiClient, run_id: str, wait: bool) -> None:
    """Cancel a run: new work stops; external work is confirmed before 'cancelled'."""
    result = api.cancel(run_id)
    run: JsonObject = result.get("run") or {}
    state = str(run.get("status"))
    if state == "cancelled":
        click.echo("Cancelled.")
    else:
        click.echo(
            f"Cancellation requested; the run is {state}. No new work will start, "
            "but in-flight external work (such as a running query) is still being "
            "confirmed stopped, so it is not yet known to be fully stopped."
        )
    if wait and state not in ("cancelled", "completed", "partial", "failed"):
        driven = drive_run(api, run_id, out=_out)
        raise SystemExit(_STATUS_EXIT.get(str(driven.run.get("status")), EXIT_ERROR))
    raise SystemExit(
        _STATUS_EXIT.get(state, 0) if state != "cancelling" else EXIT_CANCELLING
    )


@cli.command()
@json_option
@guarded
def sessions(api: ApiClient, as_json: bool) -> None:
    """List your sessions, most recent first."""
    result = api.list_sessions()
    click.echo(json.dumps(result) if as_json else format_sessions(result["sessions"]))


@cli.command()
@click.argument("session_id")
@json_option
@guarded
def runs(api: ApiClient, session_id: str, as_json: bool) -> None:
    """List a session's runs, newest first."""
    result = api.get_session(session_id)
    if as_json:
        _emit(result)
        return
    click.echo("\n".join(format_run_line(r) for r in result["runs"]) or "No runs.")


@cli.command()
@click.argument("run_id")
@json_option
@guarded
def show(api: ApiClient, run_id: str, as_json: bool) -> None:
    """Show a run's state, open question and released answer."""
    run = api.get_run(run_id)
    if as_json:
        _emit(run)
        return
    click.echo(f"run {run['run_id']}: {run['status']}")
    question = run.get("question")
    if isinstance(question, dict):
        click.echo(format_question(str(question["text"]["text"])))
    result = format_run_result(run)
    if result:
        click.echo(result)


# --- reports ---


@cli.group()
def reports() -> None:
    """Saved reports: list, search, read, export."""


@reports.command("list")
@click.option("--session", "session_id")
@json_option
@guarded
def reports_list(api: ApiClient, session_id: str | None, as_json: bool) -> None:
    """List your saved reports."""
    result = api.list_reports(session_id=session_id)
    click.echo(json.dumps(result) if as_json else format_report_list(result["reports"]))


@reports.command("search")
@click.argument("query")
@json_option
@guarded
def reports_search(api: ApiClient, query: str, as_json: bool) -> None:
    """Search report titles and content."""
    result = api.search_reports(query)
    click.echo(json.dumps(result) if as_json else format_report_search(result))


@reports.command("show")
@click.argument("report_id")
@click.option("--version", type=int)
@json_option
@guarded
def reports_show(
    api: ApiClient, report_id: str, version: int | None, as_json: bool
) -> None:
    """Read a report with its cited evidence."""
    doc = api.get_report(report_id, version)
    click.echo(json.dumps(doc) if as_json else format_report(doc))


@reports.command("versions")
@click.argument("report_id")
@guarded
def reports_versions(api: ApiClient, report_id: str) -> None:
    """List a report's versions."""
    click.echo(format_versions(api.report_versions(report_id)))


@reports.command("export")
@click.argument("report_id")
@click.option(
    "--output",
    "-o",
    type=click.Path(dir_okay=False, path_type=Path),
    help="File (default: the server's name; '-' prints).",
)
@guarded
def reports_export(api: ApiClient, report_id: str, output: Path | None) -> None:
    """Export a report as one Markdown file with its evidence appendix."""
    name, content = api.export_report(report_id)
    if output is not None and str(output) == "-":
        sys.stdout.buffer.write(content)
        return
    target = output or Path(Path(name).name)
    target.write_bytes(content)
    click.echo(f"Saved {len(content)} bytes to {target}")


# --- deletion ---


@cli.group()
def deletion() -> None:
    """Review and confirm deletions the assistant proposed."""


@deletion.command("show")
@click.argument("proposal_id")
@json_option
@guarded
def deletion_show(api: ApiClient, proposal_id: str, as_json: bool) -> None:
    """Show exactly which reports a proposal would delete."""
    preview = api.deletion_preview(proposal_id)
    click.echo(json.dumps(preview) if as_json else format_deletion_preview(preview))


@deletion.command("confirm")
@click.argument("proposal_id")
@click.option(
    "--confirm-text",
    help="Scripts only: the exact phrase to type, e.g. 'delete 2 reports'. "
    "There is no yes-to-all flag.",
)
@guarded
def deletion_confirm(
    api: ApiClient, proposal_id: str, confirm_text: str | None
) -> None:
    """Show the proposal, then delete only if you type the exact phrase."""

    def read(prompt: str) -> str | None:
        if confirm_text is not None:
            click.echo(prompt + confirm_text)
            return confirm_text
        try:
            return click.prompt(prompt.rstrip(": "), default="", show_default=False)
        except click.Abort:
            return None

    if not confirm_deletion(api, proposal_id, read_line=read, out=_out):
        raise SystemExit(EXIT_ERROR)


@deletion.command("cancel")
@click.argument("proposal_id")
@guarded
def deletion_cancel(api: ApiClient, proposal_id: str) -> None:
    """Withdraw a proposal (nothing is deleted)."""
    api.cancel_deletion(proposal_id)
    click.echo("Deletion proposal withdrawn. Nothing was deleted.")


__all__ = ["ClientFactory", "cli", "confirmation_phrase", "one_line"]
