"""scripts/local_cli.sh with stub executables in a temporary repo and HOME."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "local_cli.sh"
TOKEN = "stub-secret-token"

DEV_ACCESS = f"""#!/bin/sh
[ "$1 $2" = "token local-admin" ] || exit 9
[ -n "$STUB_FAIL" ] && {{ echo "not provisioned" >&2; exit 1; }}
echo {TOKEN}
"""
ANALYTICS = """#!/bin/sh
{
  echo "args=$*"
  echo "file=$CLI_TOKEN_FILE"
  echo "env_token=${CLI_TOKEN:-unset}"
  echo "content=$(cat "$CLI_TOKEN_FILE")"
} > "$STUB_OUT"
exit "${STUB_EXIT:-0}"
"""


def _write_exe(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o755)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / ".venv" / "bin").mkdir(parents=True)
    shutil.copy(SCRIPT, root / "scripts" / "local_cli.sh")
    _write_exe(root / ".venv" / "bin" / "retail-analytics-dev-access", DEV_ACCESS)
    _write_exe(root / ".venv" / "bin" / "analytics", ANALYTICS)
    (tmp_path / "home").mkdir()
    return root


def _run(
    repo: Path,
    *args: str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    home = repo.parent / "home"
    environment = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "STUB_OUT": str(repo.parent / "out.txt"),
        **(env or {}),
    }
    return subprocess.run(
        ["/bin/sh", str(repo / "scripts" / "local_cli.sh"), *args],
        cwd=cwd or repo.parent,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_issues_private_token_and_execs_chat_with_forwarded_args(repo: Path) -> None:
    home = repo.parent / "home"
    token_file = home / ".analytics-token"
    token_file.write_text("old")
    token_file.chmod(0o644)

    result = _run(repo, "--resume", "--session", "s-1", env={"CLI_TOKEN": "stale"})

    assert result.returncode == 0
    assert TOKEN not in result.stdout + result.stderr
    out = (repo.parent / "out.txt").read_text()
    assert "args=chat --resume --session s-1" in out
    assert f"file={token_file}" in out
    assert "env_token=unset" in out
    assert f"content={TOKEN}" in out
    assert _mode(token_file) == 0o600
    assert [p.name for p in home.iterdir()] == [".analytics-token"]


def test_exit_status_of_chat_is_preserved(repo: Path) -> None:
    assert _run(repo, env={"STUB_EXIT": "5"}).returncode == 5


def test_failed_token_generation_keeps_existing_token(repo: Path) -> None:
    home = repo.parent / "home"
    token_file = home / ".analytics-token"
    token_file.write_text("working")

    result = _run(repo, env={"STUB_FAIL": "1"})

    assert result.returncode == 1
    assert "bootstrap.sh" in result.stderr
    assert token_file.read_text() == "working"
    assert [p.name for p in home.iterdir()] == [".analytics-token"]
    assert not (repo.parent / "out.txt").exists()


def test_symlinked_token_file_is_refused(repo: Path) -> None:
    home = repo.parent / "home"
    victim = repo.parent / "victim"
    victim.write_text("keep")
    (home / ".analytics-token").symlink_to(victim)

    result = _run(repo)

    assert result.returncode == 1
    assert "symbolic link" in result.stderr
    assert victim.read_text() == "keep"
    assert not (repo.parent / "out.txt").exists()


def test_missing_virtualenv_points_to_bootstrap(repo: Path) -> None:
    shutil.rmtree(repo / ".venv")
    result = _run(repo, cwd=repo / "scripts")
    assert result.returncode == 1
    assert "bootstrap.sh" in result.stderr
