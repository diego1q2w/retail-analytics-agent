"""The interactive chat loop.

One main thread owns all output and state. A follower thread streams the
active run's events, and a reader thread supplies input lines; both feed one
inbox. On a terminal the user can type while a run works: plain text then
steers the run (or answers its open question), ``/queue`` queues a separate
request, ``/cancel`` cancels. When stdin is not a terminal (scripts, tests)
the next line is read only once the run has finished or asks a question, so
results are deterministic.

On a terminal (stdin and stdout both TTYs) the chat reads keys itself
(``terminal.LineEditor``): progress is printed above the input line and the
one active prompt is drawn again with the text typed so far, switching
between ``you>``, ``steer>`` and ``answer>`` as the run's state changes.

Ctrl-C: while a run is being followed it only DETACHES (the run keeps
working; nothing is cancelled); at an idle prompt it leaves the chat. Use
``/cancel`` to stop a run. Reopen the session to see its progress again.
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import IO, Any

from retail_analytics.interfaces.cli.client import (
    ApiClient,
    ApiError,
    JsonObject,
    Unreachable,
    new_submission_key,
)
from retail_analytics.interfaces.cli.deletion import confirm_deletion
from retail_analytics.interfaces.cli.follow import (
    DEFAULT_STALL_SECONDS,
    StreamLost,
    follow_run,
)
from retail_analytics.interfaces.cli.render import (
    EventFormatter,
    format_acknowledgement,
    format_definition_notices,
    format_deletion_preview,
    format_error,
    format_question,
    format_report,
    format_report_list,
    format_report_search,
    format_run_result,
    format_sessions,
    one_line,
)
from retail_analytics.interfaces.cli.runs import Question, open_question
from retail_analytics.interfaces.cli.terminal import LineEditor, RawTerminal

HELP = """Type a question to start an investigation. While one is running:
  <text>            steer it (or answer its question when it asks one)
  /queue <text>     ask a separate question to run after this one
  /cancel           stop the run (new work stops; external work is confirmed)
  /status           show the run's state and any open question
  /follow           re-attach to the run's progress
Other commands:
  /sessions  /new  /reports  /search <words>  /report <id> [version]
  /export <id> [file]   save a report as Markdown
  /confirm <proposal>   review and confirm a deletion the assistant proposed
  /decline <proposal>   withdraw a deletion proposal
  /help  /quit
Ctrl-C while a run works only detaches (the run keeps going); /cancel cancels."""


@dataclass(frozen=True, slots=True)
class _Item:
    kind: str
    generation: int
    payload: Any


class Chat:
    def __init__(
        self,
        api: ApiClient,
        session_id: str,
        *,
        out: Callable[[str], None],
        stdin: IO[str],
        interactive: bool,
        stall_seconds: float = DEFAULT_STALL_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        write_file: Callable[[str, bytes], None] | None = None,
        terminal: RawTerminal | None = None,
    ) -> None:
        self.api = api
        self.session_id = session_id
        self._raw_out = out
        self._format_event = EventFormatter()
        self.out = self._print
        self._terminal = terminal
        self._editor = (
            None if terminal is None else LineEditor(terminal.write, bold_prompt=True)
        )
        self._keys = ""
        self._fixed_prompt: str | None = None
        self._stdin = stdin
        self._interactive = interactive
        self._stall = stall_seconds
        self._sleep = sleep
        self._write_file = write_file or _write_file
        self._inbox: queue.Queue[_Item] = queue.Queue()
        self._want = threading.Event()
        self._reader: threading.Thread | None = None
        self._generation = 0
        self.run_id: str | None = None
        self.running = False
        self.following = False
        self.question: Question | None = None
        self.last_event_id: str | None = None
        self.queued = 0
        self._offered: set[str] = set()
        # Runs whose start this chat already acknowledged from the server's
        # receipt (their run.started is then not shown again), and runs it
        # has followed (to find a queued request's run once it exists).
        self._acknowledged: set[str] = set()
        self._followed: set[str] = set()
        self._eof = False
        self._line: str | None = None
        self._have_line = False
        self._redraw = False

    # --- lifecycle ---

    def run(self, *, resume: bool) -> int:
        if resume:
            self._attach_existing()
        while True:
            try:
                if not self._interactive:
                    self._settle()
                    if self._eof:
                        break
                line = self._read_line(self._prompt())
                if line is None:
                    if self.running and not self.question:
                        self._settle()
                    break
                if not self._dispatch(line):
                    break
            except KeyboardInterrupt:
                if self._editor is not None:
                    self._editor.interrupt()
                if self.following:
                    self._detach()
                    continue
                self.out("")
                break
        self._farewell()
        return 0

    def _farewell(self) -> None:
        self._generation += 1  # stops any follower thread; the run is untouched
        if self.running and self.run_id:
            extra = ""
            if self.question:
                extra = " It is waiting for your answer."
            self.out(
                f"Run {self.run_id} is still active and was not cancelled.{extra} "
                f"Reopen with: analytics chat --session {self.session_id}"
            )

    def _prompt(self) -> str:
        if self.question:
            return "answer> "
        if self.running:
            return "steer> "
        return "you> "

    # --- input ---

    def _turn_gap(self) -> None:
        """One blank line between a finished response and the next input.

        Interactive only (piped transcripts keep their exact text); printed
        once per finished run, never per progress event or redraw."""
        if self._interactive:
            self.out("")

    def _print(self, text: str) -> None:
        if self._editor is None:
            self._raw_out(text)
        else:
            self._editor.print_above(lambda: self._raw_out(text))

    def _start_reader(self) -> None:
        if self._reader is not None:
            return
        if self._terminal is not None:
            self._reader = threading.current_thread()  # marks it started
            self._terminal.start(lambda keys: self._inbox.put(_Item("keys", 0, keys)))
            return

        def work() -> None:
            while True:
                self._want.wait()
                self._want.clear()
                line = self._stdin.readline()
                self._inbox.put(_Item("line", 0, line if line else None))
                if not line:
                    return

        self._reader = threading.Thread(target=work, daemon=True)
        self._reader.start()

    def _read_line(self, prompt: str, *, force_prompt: bool = False) -> str | None:
        if self._editor is not None:
            return self._edit_line(self._editor, prompt, force_prompt=force_prompt)
        self._start_reader()
        self._have_line = False
        self._want.set()
        if self._interactive or force_prompt:
            _echo_prompt(prompt)
        while not self._have_line:
            self._pump_once(0.2)
            if self._interactive and not self._have_line and self._redraw:
                self._redraw = False
                _echo_prompt(self._prompt())
        line = self._line
        if line is None:
            self._eof = True
            return None
        return line.rstrip("\r\n")

    def _edit_line(
        self, editor: LineEditor, prompt: str, *, force_prompt: bool
    ) -> str | None:
        """Terminal input: one prompt, kept below any progress printed."""
        self._start_reader()
        self._fixed_prompt = prompt if force_prompt else None
        editor.begin(prompt)
        try:
            while True:
                while self._keys:
                    key, self._keys = self._keys[0], self._keys[1:]
                    done, line = editor.feed(key)
                    if done:
                        if line is None:
                            self._eof = True
                        return line
                if self._eof:
                    editor.interrupt()
                    return None
                self._pump_once(0.2)
                if editor.active:
                    editor.set_prompt(self._fixed_prompt or self._prompt())
        finally:
            self._fixed_prompt = None

    def _settle(self) -> None:
        """Non-interactive: process events until the run finished, asks for an
        answer, or nothing is in flight."""
        while (self.running and not self.question) or (
            self.queued > 0 and not self.running
        ):
            self._pump_once(0.2)

    def _pump_once(self, timeout: float) -> None:
        try:
            item = self._inbox.get(timeout=timeout)
        except queue.Empty:
            return
        if item.kind == "line":
            self._line = item.payload
            self._have_line = True
            return
        if item.kind == "keys":
            if item.payload is None:
                self._eof = True
            else:
                self._keys += item.payload
            return
        if item.generation != self._generation:
            return
        try:
            self._handle(item)
        except (ApiError, Unreachable) as error:
            self.out(format_error(error))
        if self._interactive and self._editor is None:
            self._redraw = True

    # --- stream events ---

    def _handle(self, item: _Item) -> None:
        if item.kind == "event":
            event: JsonObject = item.payload
            self.last_event_id = str(event.get("event_id") or self.last_event_id)
            text = self._format_event(event)
            if event.get("kind") == "deletion.proposed" or (
                event.get("kind") == "run.started" and self.run_id in self._acknowledged
            ):
                # A proposal is shown in full below, from the server's own
                # record; a start was already acknowledged when accepted.
                text = None
            if text:
                self.out(text)
            if event.get("kind") == "deletion.proposed":
                self._offer_deletion(str(event.get("deletion_proposal_id") or ""))
            if event.get("kind") == "input.required" and self.run_id:
                question = open_question(self.api, self.run_id, sleep=self._sleep)
                if question is not None:
                    self.question = question
                    self.out(format_question(question.text))
                    self.out("Type your answer below.")
        elif item.kind == "notice":
            self.out(item.payload)
        elif item.kind == "end":
            self._finish()
        elif item.kind == "error":
            self.following = False
            error = item.payload
            if isinstance(error, StreamLost):
                self.out(
                    "The connection to the event stream was lost. The run keeps "
                    "working on the server; /follow retries."
                )
            else:
                self.out(format_error(error))
                if isinstance(error, ApiError) and error.code in (
                    "not_found",
                    "unauthenticated",
                ):
                    self.running = False
                    self.question = None

    def _finish(self) -> None:
        finished = self.run_id
        self.following = False
        self.running = False
        self.question = None
        if finished is None:
            return
        run = self.api.get_run(finished)
        self.out(format_run_result(run))
        self._turn_gap()
        self._offer_pending_deletions()
        if self.queued > 0:
            self._start_queued(finished)

    def _offer_deletion(self, proposal_id: str) -> None:
        """Show a pending proposal the server announced, once. The preview is
        the server's own record; nothing is confirmed here."""
        if not proposal_id or proposal_id in self._offered:
            return
        try:
            preview = self.api.deletion_preview(proposal_id)
        except (ApiError, Unreachable):
            return
        self._show_proposal(preview)

    def _offer_pending_deletions(self) -> None:
        """After a run: any pending proposal not shown yet (for example when
        this chat attached after the announcing event)."""
        try:
            pending = self.api.list_pending_deletions().get("proposals") or []
        except (ApiError, Unreachable):
            return
        for preview in pending:
            self._show_proposal(preview)

    def _show_proposal(self, preview: JsonObject) -> None:
        proposal_id = str(preview.get("proposal_id", ""))
        if preview.get("status") != "pending" or proposal_id in self._offered:
            return
        self._offered.add(proposal_id)
        self.out("")
        self.out(format_deletion_preview(preview))
        self.out(
            f"Nothing is deleted yet. To review and confirm: /confirm "
            f"{proposal_id}   To withdraw it: /decline {proposal_id}"
        )

    def _start_queued(self, finished: str) -> None:
        """Follow the run the server started for a queued request: the oldest
        run newer than ``finished`` this chat has not followed yet. It may
        already have ended (a quick run); its events are then replayed."""
        for _ in range(20):
            session = self.api.get_session(self.session_id)
            runs = [str(r.get("run_id")) for r in session.get("runs") or []]
            newer = runs[: runs.index(finished)] if finished in runs else []
            fresh = [r for r in newer if r not in self._followed]
            if fresh:
                run_id = fresh[-1]  # runs are listed newest first
                self.queued -= 1
                active = next(
                    r.get("active")
                    for r in session["runs"]
                    if str(r.get("run_id")) == run_id
                )
                self.out(
                    "Your queued question is starting."
                    if active
                    else "Your queued question already ran:"
                )
                self._follow(run_id, None)
                return
            self._sleep(0.5)
        self.queued = 0
        self.out("The queued question did not start; check /status.")

    # --- following ---

    def _follow(self, run_id: str, after: str | None) -> None:
        self._generation += 1
        generation = self._generation
        self.run_id = run_id
        self.running = True
        self.following = True
        self._followed.add(run_id)
        if after is None:
            self.last_event_id = None

        def work() -> None:
            try:
                result = follow_run(
                    self.api,
                    run_id,
                    after=after,
                    on_event=lambda e: self._inbox.put(_Item("event", generation, e)),
                    on_notice=lambda t: self._inbox.put(_Item("notice", generation, t)),
                    should_stop=lambda: self._generation != generation,
                    stall_seconds=self._stall,
                    sleep=self._sleep,
                )
                if result.outcome == "end":
                    self._inbox.put(_Item("end", generation, result))
            except (ApiError, Unreachable, StreamLost) as error:
                self._inbox.put(_Item("error", generation, error))

        threading.Thread(target=work, daemon=True).start()

    def _detach(self) -> None:
        self._generation += 1
        self.following = False
        self.out(
            f"\nDetached. Run {self.run_id} keeps working (nothing was cancelled). "
            "/follow re-attaches, /cancel stops it."
        )

    def _ensure_following(self) -> None:
        if self.running and self.run_id and not self.following:
            self._follow(self.run_id, self.last_event_id)

    def _attach_existing(self) -> None:
        session = self.api.get_session(self.session_id)
        runs = session.get("runs") or []
        if not runs:
            return
        newest = runs[0]
        if newest.get("active"):
            run = self.api.get_run(str(newest["run_id"]))
            self.out(f"Resuming run {run['run_id']} ({run['status']}).")
            self._follow(str(run["run_id"]), None)
        else:
            run = self.api.get_run(str(newest["run_id"]))
            self.out(f"Last run ({run['run_id']}, {run['status']}):")
            self.out(format_run_result(run))

    # --- dispatch ---

    def _dispatch(self, line: str) -> bool:
        """Handle one input line; False to leave the chat."""
        text = line.strip()
        if not text:
            return True
        try:
            if text.startswith("/"):
                return self._command(text)
            self._say(text)
        except KeyboardInterrupt:
            self.out("\nInterrupted. Nothing further was sent.")
        except (ApiError, Unreachable) as error:
            self.out(format_error(error))
        except Exception as error:  # the session must survive any one failure
            self.out(f"error [internal]: {type(error).__name__}; the chat continues")
        return True

    def _say(self, text: str) -> None:
        if self.question and self.run_id:
            receipt = self.api.answer(
                self.run_id, self.question.question_id, text, new_submission_key()
            )
            self.out(format_acknowledgement({**receipt, "kind": "answer"}))
            self.question = None
            self._ensure_following_after()
            return
        if self.running and self.run_id:
            try:
                receipt = self.api.steer(self.run_id, text, new_submission_key())
            except ApiError as error:
                if error.code != "run_not_active":
                    raise
                # Show how the finished run ended before the new request.
                self._await_end()
                self.out("That run just finished; starting a new request.")
                self._submit(text, "steer")
                return
            self.out(format_acknowledgement({**receipt, "kind": "steering"}))
            self._ensure_following()
            return
        self._submit(text, "steer")

    def _await_end(self, seconds: float = 10.0) -> None:
        """Handle the followed run's remaining events and its end (briefly
        bounded), so that following another run drops none of them."""
        run_id = self.run_id
        deadline = time.monotonic() + seconds
        while (
            self.run_id == run_id
            and self.running
            and self.following
            and time.monotonic() < deadline
        ):
            self._pump_once(0.2)

    def _ensure_following_after(self) -> None:
        if not self.following and self.run_id:
            self._follow(self.run_id, self.last_event_id)

    def _submit(self, text: str, mode: str) -> None:
        receipt = self.api.send_message(
            self.session_id, text, new_submission_key(), mode=mode
        )
        # Acknowledged at once from the receipt, before any progress event.
        self.out(format_acknowledgement(receipt))
        if receipt.get("run_id") is None:
            self.queued += 1
            return
        run_id = str(receipt["run_id"])
        if receipt.get("kind") != "steering":
            self._acknowledged.add(run_id)
        if run_id != self.run_id or not self.running:
            self._follow(run_id, None)
        else:
            self._ensure_following()

    def _command(self, text: str) -> bool:
        name, _, rest = text.partition(" ")
        arg = rest.strip()
        name = name.lower()
        if name in ("/quit", "/exit", "/q"):
            return False
        if name == "/help":
            self.out(HELP)
        elif name == "/status":
            self._status()
        elif name == "/cancel":
            self._cancel()
        elif name == "/queue":
            if not arg:
                self.out("Usage: /queue <question>")
            elif self.running:
                self._submit(arg, "queue")
            else:
                self._submit(arg, "steer")
        elif name == "/steer":
            if arg:
                self._say(arg)
            else:
                self.out("Usage: /steer <text>")
        elif name == "/follow":
            if self.running and self.run_id:
                self._follow(self.run_id, self.last_event_id)
            else:
                self.out("No run is active.")
        elif name == "/new":
            if self.running:
                self.out("A run is active here; /cancel it or reopen later.")
            else:
                self.session_id = str(
                    self.api.open_session(new_submission_key())["session_id"]
                )
                self.out(f"New session {self.session_id}.")
        elif name == "/sessions":
            self.out(format_sessions(self.api.list_sessions()["sessions"]))
        elif name == "/reports":
            self.out(format_report_list(self.api.list_reports()["reports"]))
        elif name == "/search":
            if not arg:
                self.out("Usage: /search <words>")
            else:
                self.out(format_report_search(self.api.search_reports(arg)))
        elif name == "/report":
            self._report(arg)
        elif name == "/export":
            self._export(arg)
        elif name == "/confirm":
            self._confirm(arg)
        elif name == "/decline":
            if not arg:
                self.out("Usage: /decline <proposal id>")
            else:
                self.api.cancel_deletion(arg)
                self.out("Deletion proposal withdrawn. Nothing was deleted.")
        else:
            self.out(f"Unknown command {one_line(name)}. /help lists the commands.")
        return True

    def _status(self) -> None:
        if not self.run_id:
            self.out("No run in this chat yet.")
            return
        run = self.api.get_run(self.run_id)
        self.out(f"Run {run['run_id']}: {run['status']}")
        question = run.get("question")
        if isinstance(question, dict):
            self.out(format_question(str(question["text"]["text"])))
        if self.queued:
            self.out(f"{self.queued} queued question(s) waiting.")

    def _cancel(self) -> None:
        if not (self.running and self.run_id):
            self.out("No run is active.")
            return
        result = self.api.cancel(self.run_id)
        status = (result.get("run") or {}).get("status", "?")
        if status == "cancelled":
            self.out("Cancelled.")
        else:
            self.out(
                "Cancellation requested: no new work will start. The run is "
                f"{status} until in-flight external work (such as a query) is "
                "confirmed stopped; that is reported when it ends."
            )
        self.question = None
        self._ensure_following()

    def _report(self, arg: str) -> None:
        parts = arg.split()
        if not parts or len(parts) > 2 or (len(parts) == 2 and not parts[1].isdigit()):
            self.out("Usage: /report <report id> [version]")
            return
        version = int(parts[1]) if len(parts) == 2 else None
        self.out(format_report(self.api.get_report(parts[0], version)))

    def _export(self, arg: str) -> None:
        parts = arg.split(maxsplit=1)
        if not parts:
            self.out("Usage: /export <report id> [file]")
            return
        name, content, notices = self.api.export_report(parts[0])
        target = parts[1] if len(parts) == 2 else name
        self._write_file(target, content)
        self.out(f"Saved {len(content)} bytes to {target}.")
        if notices:
            self.out(format_definition_notices(notices))

    def _confirm(self, arg: str) -> None:
        if not arg:
            self.out("Usage: /confirm <proposal id>")
            return

        def read(prompt: str) -> str | None:
            try:
                return self._read_line(prompt, force_prompt=True)
            except KeyboardInterrupt:
                return None

        confirm_deletion(self.api, arg, read_line=read, out=self.out)


def _echo_prompt(prompt: str) -> None:
    import click

    click.echo(prompt, nl=False)


def _write_file(path: str, content: bytes) -> None:
    with open(path, "wb") as handle:
        handle.write(content)
