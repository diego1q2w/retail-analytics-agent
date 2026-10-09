"""Interactive chat input stays intact while progress arrives (T39-F1).

``LineEditor`` is tested directly; the chat itself is driven through a real
pseudo-terminal, and the bytes it writes are replayed on a small screen model
so the test checks what the user actually sees.
"""

from __future__ import annotations

import os
import re
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest

from retail_analytics.interfaces.cli.terminal import LineEditor

ROOT = Path(__file__).resolve().parents[3]


class Screen:
    """Enough of a VT100 for the sequences the chat emits."""

    def __init__(self) -> None:
        self.lines: list[str] = [""]
        self.row = 0
        self.col = 0
        self.raw = ""

    def feed(self, data: str) -> None:
        self.raw += data
        i = 0
        while i < len(data):
            ch = data[i]
            if ch == "\x1b":
                match = re.match(r"\x1b\[(\d*)([A-Za-z])", data[i:])
                if match is None:
                    i += 1
                    continue
                count = int(match.group(1) or 1)
                if match.group(2) == "A":
                    self.row = max(self.row - count, 0)
                elif match.group(2) == "J":
                    self.lines[self.row] = self.lines[self.row][: self.col]
                    del self.lines[self.row + 1 :]
                elif match.group(2) == "K":
                    self.lines[self.row] = self.lines[self.row][: self.col]
                i += match.end()
                continue
            if ch == "\r":
                self.col = 0
            elif ch == "\n":
                self.row += 1
                self.col = 0
                while len(self.lines) <= self.row:
                    self.lines.append("")
            elif ch == "\b":
                self.col = max(self.col - 1, 0)
            elif ch.isprintable():
                line = self.lines[self.row].ljust(self.col)
                self.lines[self.row] = line[: self.col] + ch + line[self.col + 1 :]
                self.col += 1
            i += 1

    def visible(self) -> list[str]:
        return [line.rstrip() for line in self.lines]


# --- LineEditor --------------------------------------------------------------


def editor() -> tuple[LineEditor, Screen]:
    screen = Screen()
    return LineEditor(screen.feed, columns=lambda: 200), screen


def type_text(line_editor: LineEditor, text: str) -> tuple[bool, str | None]:
    result: tuple[bool, str | None] = (False, "")
    for key in text:
        result = line_editor.feed(key)
    return result


def test_output_is_printed_above_the_line_being_typed() -> None:
    line_editor, screen = editor()
    line_editor.begin("steer> ")
    type_text(line_editor, "and by sta")
    line_editor.print_above(lambda: screen.feed("Running a query.\n"))
    line_editor.print_above(lambda: screen.feed("Checking the evidence.\n"))
    assert screen.visible() == [
        "Running a query.",
        "Checking the evidence.",
        "steer> and by sta",
    ]
    assert type_text(line_editor, "te?\r") == (True, "and by state?")
    assert not line_editor.active


def test_prompt_switches_keep_the_typed_text() -> None:
    line_editor, screen = editor()
    line_editor.begin("steer> ")
    type_text(line_editor, "last")
    line_editor.set_prompt("answer> ")
    assert screen.visible() == ["answer> last"]


def test_editing_keys_and_escape_sequences() -> None:
    line_editor, screen = editor()
    line_editor.begin("you> ")
    type_text(line_editor, "revenu\x7fue\x1b[Dby\x17 by month\x15orders")
    assert screen.visible() == ["you> orders"]
    assert line_editor.text == "orders"
    assert type_text(line_editor, "\x15\x04") == (True, None)


def test_wrapped_input_is_erased_completely() -> None:
    screen = Screen()
    line_editor = LineEditor(screen.feed, columns=lambda: 10)
    line_editor.begin("you> ")
    type_text(line_editor, "a long question")  # 20 characters: two rows
    screen.lines = ["you> a lon", "g question"]
    screen.row, screen.col = 1, 10
    line_editor.print_above(lambda: screen.feed("progress\n"))
    assert screen.visible()[:2] == ["progress", "you> a long question"]


# --- the chat on a real pseudo-terminal -----------------------------------------


class Pty:
    def __init__(self) -> None:
        import fcntl
        import pty
        import struct
        import termios

        env = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                [str(ROOT / "src"), str(ROOT), os.environ.get("PYTHONPATH", "")]
            ),
            "TERM": "xterm",
        }
        fd, child = pty.openpty()
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 200, 0, 0))
        # A new session; the driver makes this terminal its controlling one,
        # so Ctrl-C reaches it as SIGINT like in a real shell.
        self.process = subprocess.Popen(
            [sys.executable, "-m", "tests.unit.cli.pty_chat_driver"],
            stdin=child,
            stdout=child,
            stderr=child,
            env=env,
            cwd=ROOT,
            start_new_session=True,
        )
        os.close(child)
        self.fd = fd
        self.screen = Screen()

    def read_until(self, needle: str, seconds: float = 20, count: int = 1) -> None:
        deadline = time.monotonic() + seconds
        while self.screen.raw.count(needle) < count:
            left = deadline - time.monotonic()
            if left <= 0:
                raise AssertionError(
                    f"timed out waiting for {needle!r}; screen:\n"
                    + "\n".join(self.screen.visible())
                )
            ready, _, _ = select.select([self.fd], [], [], min(left, 0.2))
            if ready:
                try:
                    data = os.read(self.fd, 4096)
                except OSError:
                    break
                self.screen.feed(data.decode("utf-8", "replace"))

    def settle(self, seconds: float = 0.4) -> None:
        deadline = time.monotonic() + seconds
        while (left := deadline - time.monotonic()) > 0:
            ready, _, _ = select.select([self.fd], [], [], left)
            if ready:
                try:
                    data = os.read(self.fd, 4096)
                except OSError:
                    return
                self.screen.feed(data.decode("utf-8", "replace"))

    def type(self, text: str) -> None:
        for key in text:
            os.write(self.fd, key.encode())
            time.sleep(0.01)

    def close(self) -> int:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            code = self.process.poll()
            if code is not None:
                os.close(self.fd)
                return code
            self.settle(0.1)
        self.process.kill()
        self.process.wait()
        os.close(self.fd)
        raise AssertionError("the chat did not exit")


def prompts(lines: list[str]) -> list[str]:
    return [line for line in lines if re.match(r"^(you|steer|answer)>( |$)", line)]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pseudo-terminal")
def test_chat_on_a_terminal_keeps_one_prompt_and_the_typed_text() -> None:
    term = Pty()
    try:
        term.read_until("you> ")
        term.type("How much revenue?\r")
        term.read_until("Working on it.")
        term.type("and by sta")  # typing while the run reports progress
        term.read_until("Running a query.")
        term.settle()
        lines = term.screen.visible()
        # Progress lines are clean, the partial input appears once, at the
        # bottom, under the single active prompt.
        assert "  > Running a query." in lines
        assert lines[-1] == "steer> and by sta"
        assert sum("and by sta" in line for line in lines) == 1
        assert prompts(lines) == ["you> How much revenue?", "steer> and by sta"]

        # A clarification switches the prompt to answer input, text intact.
        term.read_until("Type your answer below.")
        term.settle()
        lines = term.screen.visible()
        assert lines[-1] == "answer> and by sta"
        assert prompts(lines)[-1] == "answer> and by sta"
        term.type("\x15last month\r")
        term.read_until("Revenue was 10.")
        term.settle()
        lines = term.screen.visible()
        assert "  > Checking the evidence." in lines
        assert lines[-1] == "you>"  # back to idle, one prompt
        assert prompts(lines) == [
            "you> How much revenue?",
            "answer> last month",
            "you>",
        ]

        # Ctrl-C while following detaches; /cancel still cancels.
        term.type("second question\r")
        term.read_until("Working on it.", count=2)
        term.type("half typed")
        term.type("\x03")
        term.read_until("Detached.")
        term.settle()
        assert term.screen.visible()[-1] == "steer>"
        term.type("/cancel\r")
        term.read_until("Cancelled.")
        term.read_until("cancelled")
        term.settle()
        assert term.screen.visible()[-1] == "you>"
        term.type("/quit\r")
        term.read_until("TERMINAL-RESTORED=")
    finally:
        code = term.close()
    assert code == 0
    assert "TERMINAL-RESTORED=True" in term.screen.raw
