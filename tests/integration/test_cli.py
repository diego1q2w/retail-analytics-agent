"""The ``analytics`` CLI as a real subprocess against a real API and worker.

Same stack as the HTTP API test (PostgreSQL + Temporal in Docker, uvicorn in
this process, a worker subprocess with the scripted model);
``test_local_cli`` runs the same tests with local execution. No live model
calls. The CLI is exercised only through its public commands; a small TCP
proxy lets one test drop the event stream in the middle of a run.
"""

# ruff: noqa: F811
from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_asyncio

from retail_analytics.application.contracts.authorization import Principal
from tests.integration.test_http_api import (  # noqa: F401  (fixtures reused)
    Api,
    api,
    backend,
    bearer,
    world,
)
from tests.integration.test_report_deletion import World

pytestmark = [pytest.mark.docker, pytest.mark.asyncio]
ROOT = Path(__file__).resolve().parents[2]
QUESTION = "Which sales period should I use?"
ANNUAL = "The updated annual sales investigation is complete."
PLAIN = "The requested sales investigation is complete."


class Cli:
    """Runs ``analytics`` for one executive in a clean, isolated environment."""

    def __init__(self, url: str, principal: Principal, tmp: Path) -> None:
        (tmp / "empty.env").write_text("")
        self.env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(ROOT / "src"),
            "RETAIL_ANALYTICS_ENV_FILE": str(tmp / "empty.env"),
            "ANALYTICS_CLI_API_URL": url,
            "ANALYTICS_CLI_TOKEN": bearer(principal)["Authorization"].removeprefix(
                "Bearer "
            ),
            "ANALYTICS_CLI_TIMEOUT_SECONDS": "30",
        }

    def popen(self, *args: str, url: str | None = None) -> subprocess.Popen[str]:
        env = dict(self.env)
        if url:
            env["ANALYTICS_CLI_API_URL"] = url
        return subprocess.Popen(
            [sys.executable, "-m", "retail_analytics.bootstrap.cli", *args],
            env=env,
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def run(
        self, *args: str, stdin: str = "", url: str | None = None, timeout: int = 120
    ) -> subprocess.CompletedProcess[str]:
        process = self.popen(*args, url=url)
        out, _ = process.communicate(stdin, timeout=timeout)
        return subprocess.CompletedProcess(args, process.returncode, out, None)

    def start_run(self, text: str) -> str:
        """Start a run without waiting (retrying while Temporal warms up)."""
        deadline = time.monotonic() + 60
        while True:
            done = self.run("ask", text, "--no-wait", "--json")
            if done.returncode == 0:
                return str(json.loads(done.stdout)["run_id"])
            assert time.monotonic() < deadline, done.stdout
            time.sleep(1)


@pytest_asyncio.fixture
async def alice(api: Api, world: World, tmp_path: Path) -> tuple[Cli, Principal, str]:
    principal, session = await world.executive({"1", "2", "3"})
    return Cli(api.base_url, principal, tmp_path), principal, session


class DropOnceProxy:
    """A TCP proxy to the API that cuts the first event-stream connection
    after it delivered some events, like a network failure."""

    def __init__(self, target_port: int) -> None:
        self._target = target_port
        self._server = socket.socket()
        self._server.bind(("127.0.0.1", 0))
        self._server.listen()
        self.port = self._server.getsockname()[1]
        self.dropped = 0
        self.stream_requests: list[bytes] = []
        self._lock = threading.Lock()
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self) -> None:
        self._server.close()

    def _accept(self) -> None:
        while True:
            try:
                client, _ = self._server.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client: socket.socket) -> None:
        with (
            closing(client),
            closing(socket.create_connection(("127.0.0.1", self._target))) as upstream,
        ):
            request = client.recv(65536)
            upstream.sendall(request)
            is_stream = b"/events" in request.split(b"\r\n", 1)[0]
            if is_stream:
                self.stream_requests.append(request)
            threading.Thread(
                target=self._pipe_requests, args=(client, upstream), daemon=True
            ).start()
            while True:
                chunk = upstream.recv(65536)
                if not chunk:
                    return
                try:
                    client.sendall(chunk)
                except OSError:
                    return
                with self._lock:
                    if is_stream and self.dropped == 0 and b"event:" in chunk:
                        self.dropped += 1
                        return

    @staticmethod
    def _pipe_requests(client: socket.socket, upstream: socket.socket) -> None:
        try:
            while data := client.recv(65536):
                upstream.sendall(data)
        except OSError:
            return


def _lines(output: str) -> list[str]:
    return [line for line in output.splitlines() if line.strip()]


async def test_cli_reconnects_mid_run_with_last_event_id_without_duplicates(
    api: Api, alice: tuple[Cli, Principal, str]
) -> None:
    cli, _principal, _ = alice
    run_id = await asyncio.to_thread(cli.start_run, "Analyze sales: slow case.")
    port = int(api.base_url.rsplit(":", 1)[1])
    proxy = DropOnceProxy(port)
    try:
        done = await asyncio.to_thread(
            cli.run, "follow", run_id, url=f"http://127.0.0.1:{proxy.port}"
        )
    finally:
        proxy.close()
    assert done.returncode == 0, done.stdout
    assert proxy.dropped == 1, "the connection was supposed to be cut once"
    assert "reconnecting" in done.stdout
    assert PLAIN in done.stdout
    # The cut stream resumed from the last event: the output is exactly what an
    # uninterrupted follow shows (nothing missing, nothing repeated).
    clean = await asyncio.to_thread(cli.run, "follow", run_id)
    seen = [ln for ln in _lines(done.stdout) if "reconnecting" not in ln]
    assert seen == _lines(clean.stdout)
    assert len(_lines(clean.stdout)) >= 2
    assert len(proxy.stream_requests) >= 2
    assert b"last-event-id" not in proxy.stream_requests[0].lower()
    assert b"last-event-id: " in proxy.stream_requests[1].lower()
    assert done.stdout.count(PLAIN) == 1


async def test_new_cli_process_attaches_to_in_progress_run_and_disconnect_keeps_it(
    api: Api, alice: tuple[Cli, Principal, str]
) -> None:
    cli, _principal, _ = alice
    run_id = await asyncio.to_thread(cli.start_run, "Analyze sales: slow case.")
    # First process attaches, then is killed mid-run (a closed terminal).
    first = cli.popen("follow", run_id)
    assert first.stdout is not None
    await asyncio.to_thread(first.stdout.readline)
    first.send_signal(signal.SIGKILL)
    first.communicate()
    # A brand-new process attaches to the same, still working run.
    attached = await asyncio.to_thread(cli.run, "follow", run_id)
    assert attached.returncode == 0, attached.stdout
    assert PLAIN in attached.stdout
    shown = await asyncio.to_thread(cli.run, "show", run_id, "--json")
    assert json.loads(shown.stdout)["status"] == "completed"


async def test_cli_answers_a_clarification_question(
    alice: tuple[Cli, Principal, str],
) -> None:
    cli, _principal, _ = alice
    run_id = await asyncio.to_thread(
        cli.start_run, "Analyze sales: clarification case."
    )
    waiting = await asyncio.to_thread(cli.run, "follow", run_id)
    assert waiting.returncode == 4, waiting.stdout
    assert QUESTION in waiting.stdout
    # Reopen from a new process: the question is still pending.
    shown = await asyncio.to_thread(cli.run, "show", run_id)
    assert QUESTION in shown.stdout
    answered = await asyncio.to_thread(cli.run, "answer", run_id, "use annual sales")
    assert answered.returncode == 0, answered.stdout
    assert ANNUAL in answered.stdout


async def test_cli_cancel_stops_the_run_and_reports_its_state(
    alice: tuple[Cli, Principal, str],
) -> None:
    cli, _principal, _ = alice
    run_id = await asyncio.to_thread(cli.start_run, "Analyze sales: slow case.")
    cancelled = await asyncio.to_thread(cli.run, "cancel", run_id, "--wait")
    assert cancelled.returncode in (6, 7), cancelled.stdout
    assert "ancel" in cancelled.stdout
    deadline = time.monotonic() + 30
    status = ""
    while time.monotonic() < deadline:
        shown = await asyncio.to_thread(cli.run, "show", run_id, "--json")
        status = json.loads(shown.stdout)["status"]
        if status != "cancelling":
            break
        await asyncio.sleep(0.5)
    assert status == "cancelled"
    assert "no new work" in cancelled.stdout.lower() or "Cancelled" in cancelled.stdout


async def test_deletion_needs_typed_phrase_and_declined_confirmation_deletes_nothing(
    alice: tuple[Cli, Principal, str],
    world: World,
) -> None:
    cli, principal, session = alice
    one = await world.report(principal, session, "Client X revenue")
    two = await world.report(principal, session, "Client X churn")
    proposal = await world.propose(principal, session, one)
    pid = proposal.proposal_id

    declined = await asyncio.to_thread(
        cli.run, "deletion", "confirm", pid, stdin="no\n"
    )
    assert declined.returncode == 1, declined.stdout
    assert "Client X revenue" in declined.stdout and one in declined.stdout
    assert "Nothing was deleted." in declined.stdout
    assert world.live_ids(principal) == {one, two}

    wrong_count = await asyncio.to_thread(
        cli.run, "deletion", "confirm", pid, stdin="delete 2 reports\n"
    )
    assert wrong_count.returncode == 1
    assert world.live_ids(principal) == {one, two}

    confirmed = await asyncio.to_thread(
        cli.run, "deletion", "confirm", pid, stdin="delete 1 report\n"
    )
    assert confirmed.returncode == 0, confirmed.stdout
    assert "Deleted 1 report(s)" in confirmed.stdout
    assert world.live_ids(principal) == {two}
    listed = await asyncio.to_thread(cli.run, "reports", "list")
    assert one not in listed.stdout and two in listed.stdout
    replay = await asyncio.to_thread(
        cli.run, "deletion", "confirm", pid, stdin="delete 1 report\n"
    )
    assert replay.returncode == 1


async def test_cli_lists_only_own_pending_unexpired_proposals(
    alice: tuple[Cli, Principal, str],
    world: World,
    api: Api,
    tmp_path: Path,
) -> None:
    cli, principal, session = alice
    ids = [await world.report(principal, session, f"Report {n}") for n in range(4)]
    pending = await world.propose(principal, session, ids[0])
    confirmed = await world.propose(principal, session, ids[1])
    await world.deletion.confirm(principal, confirmed.proposal_id)
    cancelled = await world.propose(principal, session, ids[2])
    await world.deletion.cancel(principal, cancelled.proposal_id)
    world.clock.advance(-timedelta(hours=1))
    expired = await world.propose(principal, session, ids[3])
    world.clock.advance(timedelta(hours=1))
    bob, bob_session = await world.executive({"7", "8"})
    bob_report = await world.report(bob, bob_session, "Bob report")
    bobs = await world.propose(bob, bob_session, bob_report)

    mine = await asyncio.to_thread(cli.run, "deletion", "list", "--json")
    assert mine.returncode == 0, mine.stdout
    listed = json.loads(mine.stdout)["proposals"]
    assert [p["proposal_id"] for p in listed] == [pending.proposal_id]
    assert [i["title"] for i in listed[0]["items"]] == ["Report 0"]
    for hidden in (confirmed, cancelled, expired, bobs):
        assert hidden.proposal_id not in mine.stdout
    (tmp_path / "bob").mkdir()
    bobs_cli = Cli(api.base_url, bob, tmp_path / "bob")
    theirs = await asyncio.to_thread(bobs_cli.run, "deletion", "list", "--json")
    assert [p["proposal_id"] for p in json.loads(theirs.stdout)["proposals"]] == [
        bobs.proposal_id
    ]
    text = await asyncio.to_thread(cli.run, "deletion", "list")
    assert "Report 0" in text.stdout and "Nothing" not in text.stdout
    # Listing confirms and deletes nothing.
    assert world.live_ids(principal) == {ids[0], ids[2], ids[3]}
    rows = world.sql(
        "SELECT run_id, payload FROM run_events WHERE kind = 'deletion.proposed' "
        "AND payload->>'deletion_proposal_id' = %s",
        pending.proposal_id,
    )
    assert len(rows) == 1
    payload = rows[0][1]
    assert isinstance(payload, dict)
    assert "Report 0" not in json.dumps(payload) and ids[0] not in json.dumps(payload)


async def test_chat_surfaces_a_proposal_without_parsing_answer_text(
    alice: tuple[Cli, Principal, str],
    world: World,
) -> None:
    cli, principal, session = alice
    report = await world.report(principal, session, "Quarterly revenue")
    proposal = await world.propose(principal, session, report)
    pid = proposal.proposal_id
    lines = "\n".join(
        ["Analyze sales for the follow-up.", f"/confirm {pid}", "no", "/quit"]
    )
    done = await asyncio.to_thread(cli.run, "chat", stdin=lines + "\n", timeout=240)
    assert done.returncode == 0, done.stdout
    out = done.stdout
    assert PLAIN in out
    assert pid not in PLAIN
    shown = out.index("Nothing is deleted yet")
    assert out.count("Nothing is deleted yet") == 1
    assert f"/confirm {pid}" in out[shown:]
    assert "Quarterly revenue" in out[:shown]
    assert "Nothing was deleted." in out
    assert world.live_ids(principal) == {report}


async def test_scripted_chat_session_ask_follow_up_report_and_delete(
    alice: tuple[Cli, Principal, str],
    world: World,
) -> None:
    cli, principal, session = alice
    report = await world.report(principal, session, "Quarterly revenue")
    proposal = await world.propose(principal, session, report)
    lines = "\n".join(
        [
            "Analyze sales: clarification case.",
            "use annual sales",
            "Analyze sales for the follow-up.",
            "/reports",
            f"/report {report}",
            "/bogus",
            f"/confirm {proposal.proposal_id}",
            "delete 1 report",
            "/reports",
            "/quit",
        ]
    )
    done = await asyncio.to_thread(cli.run, "chat", stdin=lines + "\n", timeout=240)
    assert done.returncode == 0, done.stdout
    out = done.stdout
    assert QUESTION in out
    assert ANNUAL in out
    assert PLAIN in out  # the follow-up run
    assert "Quarterly revenue" in out
    assert "Unknown command /bogus" in out
    assert "Deleted 1 report(s)" in out
    assert world.live_ids(principal) == set()
    assert cli.env["ANALYTICS_CLI_TOKEN"] not in out


def _read_until(fd: int, needle: str, seconds: float = 60) -> str:
    import select

    seen = ""
    deadline = time.monotonic() + seconds
    while needle not in seen:
        assert time.monotonic() < deadline, f"timed out waiting for {needle!r}: {seen}"
        ready, _, _ = select.select([fd], [], [], 0.5)
        if ready:
            try:
                data = os.read(fd, 4096)
            except OSError:
                break
            seen += data.decode(errors="replace")
    return seen


async def test_terminal_session_steers_then_ctrl_c_detaches_without_cancelling(
    alice: tuple[Cli, Principal, str],
) -> None:
    import pty

    cli, _principal, _ = alice
    master, slave = pty.openpty()
    process = subprocess.Popen(  # noqa: ASYNC220
        [sys.executable, "-m", "retail_analytics.bootstrap.cli", "chat"],
        env=cli.env,
        cwd=ROOT,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        close_fds=True,
    )
    os.close(slave)
    try:
        await asyncio.to_thread(_read_until, master, "Session ")
        # Temporal may still be warming up: the CLI resends the same key.
        os.write(master, b"Analyze sales: slow case.\n")
        await asyncio.to_thread(_read_until, master, "Working on it.")
        os.write(master, b"please focus on women's products\n")
        steered = await asyncio.to_thread(_read_until, master, "steering")
        assert "Sent as steering" in steered
        process.send_signal(signal.SIGINT)
        detached = await asyncio.to_thread(_read_until, master, "Detached")
        assert "nothing was cancelled" in detached
        os.write(master, b"/quit\n")
        left = await asyncio.to_thread(_read_until, master, "not cancelled")
        assert "still active" in left
        process.wait(timeout=30)
    finally:
        process.kill()
        os.close(master)
    run_id = next(word for word in left.split() if word.startswith("run_"))
    done = await asyncio.to_thread(cli.run, "follow", run_id)
    assert done.returncode == 0, done.stdout
    assert "complete" in done.stdout
