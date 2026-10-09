"""Line input that stays intact while progress is printed (interactive chat).

With a cooked terminal, text printed by another thread lands in the middle of
what the user is typing, and reprinting the prompt after each event stacks
prompts. On a terminal the chat therefore reads keys itself (no canonical
mode, no echo; signals such as Ctrl-C keep working) and owns the input line:

- ``LineEditor`` keeps the prompt and the typed text; printing "above" it
  clears the input line, prints, and draws the one active prompt again with
  the text typed so far. It writes plain ANSI erase/cursor-up sequences only.
- ``RawTerminal`` switches the terminal mode, reads keys on a thread and
  restores the mode on exit.

Editing is deliberately small: printable text, Backspace, Ctrl-U (clear),
Ctrl-W (delete word), Enter, Ctrl-D on an empty line (end). Arrow and other
escape keys are ignored. Non-interactive use never gets here and so never
receives control sequences.
"""

from __future__ import annotations

import codecs
import contextlib
import os
import shutil
import sys
import threading
from collections.abc import Callable
from types import TracebackType
from typing import IO, Any, Self

_CLEAR = "\r\x1b[J"
_BACKSPACES = frozenset({"\x7f", "\x08"})


class LineEditor:
    """One input line under a prompt; all writes go through ``write``."""

    def __init__(
        self,
        write: Callable[[str], None],
        *,
        columns: Callable[[], int] = lambda: shutil.get_terminal_size().columns,
    ) -> None:
        self._write = write
        self._columns = columns
        self.prompt: str | None = None
        self._buffer: list[str] = []
        self._escape = ""

    @property
    def active(self) -> bool:
        return self.prompt is not None

    @property
    def text(self) -> str:
        return "".join(self._buffer)

    def begin(self, prompt: str) -> None:
        """Show ``prompt`` and start a new line (typed-ahead keys follow)."""
        self.prompt = prompt
        self._buffer = []
        self._write(prompt)

    def set_prompt(self, prompt: str) -> None:
        """Switch the active prompt (e.g. ``steer>`` to ``answer>``), keeping
        the typed text."""
        if self.prompt is not None and prompt != self.prompt:
            self._erase()
            self.prompt = prompt
            self._draw()

    def print_above(self, emit: Callable[[], None]) -> None:
        """Run ``emit`` (which prints whole lines) above the input line."""
        if self.prompt is None:
            emit()
            return
        self._erase()
        emit()
        self._draw()

    def interrupt(self) -> None:
        """Ctrl-C: abandon the line being typed."""
        if self.prompt is not None:
            self._write("^C\n")
        self.prompt = None
        self._buffer = []
        self._escape = ""

    def feed(self, key: str) -> tuple[bool, str | None]:
        """Apply one key. Returns (done, line); line None means end of input."""
        if self._escape:
            self._escape += key
            if _escape_finished(self._escape):
                self._escape = ""
            return False, ""
        if key == "\x1b":
            self._escape = key
        elif key in ("\r", "\n"):
            line = self.text
            self._write("\n")
            self.prompt = None
            self._buffer = []
            return True, line
        elif key == "\x04":
            if not self._buffer:
                self._write("\n")
                self.prompt = None
                return True, None
        elif key in _BACKSPACES:
            if self._buffer:
                self._buffer.pop()
                self._redraw()
        elif key == "\x15":
            self._buffer = []
            self._redraw()
        elif key == "\x17":
            text = self.text.rstrip()
            cut = text.rfind(" ") + 1
            self._buffer = list(text[:cut])
            self._redraw()
        elif key.isprintable():
            self._buffer.append(key)
            self._write(key)
        return False, ""

    def _redraw(self) -> None:
        self._erase()
        self._draw()

    def _draw(self) -> None:
        self._write(f"{self.prompt or ''}{self.text}")

    def _erase(self) -> None:
        # The input may have wrapped: go up to its first row, then erase down.
        used = len(self.prompt or "") + len(self._buffer)
        columns = max(self._columns(), 1)
        rows_up = (used - 1) // columns if used > 0 else 0
        self._write(("\r" + f"\x1b[{rows_up}A" if rows_up else "") + _CLEAR)


def _escape_finished(sequence: str) -> bool:
    """ESC + one key (Alt-key), or ESC [ / ESC O ... up to its final byte."""
    if len(sequence) == 2:
        return sequence[1] not in "[O"
    return "@" <= sequence[-1] <= "~" or len(sequence) > 16


class RawTerminal:
    """Key-at-a-time input on a POSIX terminal; restores the mode on exit."""

    def __init__(self, stdin: IO[str], stdout: IO[str]) -> None:
        self._fd = stdin.fileno()
        self._stdout = stdout
        self._saved: list[Any] | None = None
        self._reader: threading.Thread | None = None

    @classmethod
    def available(cls, stdin: IO[str], stdout: IO[str]) -> bool:
        try:
            import termios  # noqa: F401
        except ImportError:
            return False
        try:
            return stdin.isatty() and stdout.isatty()
        except (AttributeError, ValueError):
            return False

    def __enter__(self) -> Self:
        import termios

        self._saved = termios.tcgetattr(self._fd)
        mode = termios.tcgetattr(self._fd)
        mode[3] &= ~(termios.ICANON | termios.ECHO)  # signals (ISIG) stay on
        mode[6][termios.VMIN] = 1
        mode[6][termios.VTIME] = 0
        termios.tcsetattr(self._fd, termios.TCSANOW, mode)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.restore()

    def restore(self) -> None:
        if self._saved is None:
            return
        import termios

        with contextlib.suppress(termios.error, OSError):
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)
        self._saved = None

    def write(self, text: str) -> None:
        self._stdout.write(text)
        self._stdout.flush()

    def start(self, on_keys: Callable[[str | None], None]) -> None:
        """Read keys on a daemon thread; ``None`` reports end of input."""
        if self._reader is not None:
            return
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

        def work() -> None:
            while True:
                try:
                    data = os.read(self._fd, 1024)
                except OSError:
                    data = b""
                if not data:
                    on_keys(None)
                    return
                text = decoder.decode(data)
                if text:
                    on_keys(text)

        self._reader = threading.Thread(target=work, daemon=True)
        self._reader.start()


def stdio_terminal() -> RawTerminal | None:
    """The raw terminal for this process's stdio, or None when not a TTY."""
    if RawTerminal.available(sys.stdin, sys.stdout):
        return RawTerminal(sys.stdin, sys.stdout)
    return None
